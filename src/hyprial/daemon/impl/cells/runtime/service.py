"""The cell runtime service: the one ``CellRuntimePort`` implementation.

Decision notes (phase A, S3):

- Callers never touch OrgFS.  ``ensure``/``update``/``retire`` validate,
  commit to the local cache as dirty, publish a new index and kick the
  ``cell-runtime`` thread; ``index``/``lookup`` read the published snapshot.
- Only the local authority may author a cell (plan Q10).  The runtime has
  two principals (contract OC0 §1.4): ``identity.authority`` (``user:u.<sub>``)
  derives ids, is written into documents and decides what is local;
  ``identity.writer`` (``user:<username>``, the local OrgFS principal) is
  what OrgFS records as author and what ``write_spaces`` takes.  There is
  no ``author`` attribute: every caller reads the principal it means.
- Writer bindings (§1.4.5, §1.4.6): each pass takes one snapshot first,
  under the pass lock: ``writer_bindings()`` minus any entry for the local
  authority, plus the local authority -> writer.  A raising call keeps the
  previous snapshot (empty when none) and logs ``cell.writer_bindings_failed``.
  When the snapshot differs from the previous pass's, every remembered
  rejection (mirror and ``_seen``) is forgotten before any read
  (``cell.writer_bindings_changed``), so no result cached under old
  bindings survives a pass.  Held cells are never evicted by a change.
- The runtime thread (or a test calling ``run_once``) flushes dirty cells to
  every space their scope maps to, then refreshes from OrgFS: every kind by
  directory scan.  (Lazy kinds, fetched by id on a lookup miss, were removed
  with the ``conversation`` kind, the only one that ever used them.)
- Merge rule: a verified remote copy with a higher revision wins, including
  over a dirty local intent (another device of the same owner moved first;
  the intent is dropped and logged).  At an equal revision the copy with the
  higher (updatedAtMs, document text) wins, so every reader converges on one
  copy even when two spaces diverge.  A ``stale-write`` re-reads that space
  and re-applies the same rule, then retries with the fresh version.
- Dirty cells flush to every write space independently: one blocked or
  failed space never stops the others, and the cell is clean only when
  every space took it.
- One id, one authority.  Keyed ids derive from their authority, so they
  cannot collide.  A subject id (``a.`` agent) carries no authority, and
  nothing binds a subject to its owner at read time in phase A, so a
  subject-bound copy is indexed only when the local authority is its
  authority; any other authority's copy is ignored.  A local copy displaces
  a foreign-held cell of the same id; a foreign copy never displaces a held
  cell (``cell.authority_conflict``).
- Unadoptable documents are read-only (contract OC0 D1, §1.10): nothing
  removes, rewrites or overwrites a document readers will not adopt (a
  ``Rejection``, or another authority's verified copy at a held cell's
  path).  The (space, cell) copy is blocked instead: ``_blocked`` (memory
  only) keeps the version seen, ``cell.document_blocked`` is logged once per
  (space, cell, version), the cell stays dirty and indexed, and every pass
  tries the write once more; OrgFS refuses it with ``stale-write``.  When an
  agent moves the document away (``hyprial fs rm``, migration guide), the
  next write succeeds and logs ``cell.document_unblocked``.  A gone document
  (the re-read finds nothing) is not a rewrite: its copy record is dropped
  and the write retries with ``create_only``.  A refresh that sees an
  unadoptable document where a clean held cell has another copy version
  marks the cell dirty locally (the local dirty flag only), which is what
  writes it back after the document is moved.
- (space, cell) versions that were read and not adopted are remembered in
  memory, so an unchanged foreign or untrusted document is not re-read on
  every scan.
- ``org`` scope without a name means every live org space: a clean cell is
  re-marked dirty when a write space has no copy of it yet, so a newly
  joined org receives the agent cells.
- One cache, one identity (owner ruling 2026-11-21): there is no
  owner-change re-mint and no alias, ``lookup`` reads the held cell only,
  and ``ensure`` never reclaims an id held under another authority.  The
  composition refuses a cache of another identity before this class runs
  (``cache_identity``), so those paths had become unreachable.
- Hooks registered with ``on_index`` run after a pass that changed the
  index, in registration order, each fenced; a raising hook is logged.
- I5-1 source health: a spaces failure (``read_spaces``/``write_spaces``
  raising) or a failed space scan marks the source stale; a pass whose
  refresh completes with no space failure publishes the recovery.  The value
  is memory-only, ``health``/``on_health`` mirror ``index``/``on_index``
  (fenced hooks, registration order), and a health change notifies without
  waiting for a new index generation — that is the point of the separate
  hook.  Per-document write retries stay write failures, not staleness.
- Every failed write is logged (``cell.write_failed``, once per cell, space
  and pass) and leaves the cell dirty; the next pass retries.  Nothing here
  raises into the runtime loop.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from hyprial.daemon.impl.cells.model import (
    EAGER_KINDS,
    Cell,
    CellError,
    CellMember,
    CellPermissions,
)
from hyprial.daemon.impl.cells.permissions import require_authority
from hyprial.daemon.impl.cells.ports import (
    CellIdentity,
    CellIndex,
    CellRuntimeHealth,
    CellSpacesPort,
    CellSpec,
    HealthHook,
    IndexHook,
    SpaceRef,
    WriterBindings,
)
from hyprial.daemon.impl.cells.runtime.cache import CellCache
from hyprial.daemon.impl.cells.codec import encode
from hyprial.daemon.impl.cells.runtime.mirror import (
    CellMirror,
    ReadResult,
    Rejection,
    RemoteCopy,
    WriterOf,
)
from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError
from hyprial.kernel import mint_cell_id

DEFAULT_INTERVAL_S = 5.0
WRITE_ATTEMPTS = 3
THREAD_NAME = "cell-runtime"


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


def _no_bindings() -> Mapping[str, str]:
    return {}


class CellRuntime:
    """Generic cell runtime over a local cache and an OrgFS mirror."""

    def __init__(
        self,
        *,
        fs: OrgFs,
        spaces: CellSpacesPort,
        cache: CellCache,
        identity: CellIdentity,
        writer_bindings: WriterBindings = _no_bindings,
        clock_ms: Callable[[], int] = _wall_ms,
        logger: Callable[..., None] | None = None,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self._spaces = spaces
        self._cache = cache
        self._identity = identity
        self._authority = identity.authority
        self._writer_bindings = writer_bindings
        self._bindings_snapshot: dict[str, str] = {}
        self._pass_bindings: dict[str, str] | None = None
        self._blocked: dict[tuple[str, str], str] = {}
        self._clock = clock_ms
        self._logger = logger
        self._interval = interval_s
        self._mirror = CellMirror(fs, logger=logger)
        self._lock = threading.RLock()
        self._pass_lock = threading.Lock()
        self._cells: dict[str, Cell] = {}
        self._dirty: set[str] = set()
        self._seen: dict[tuple[str, str], str] = {}
        self._hooks: list[IndexHook] = []
        self._hooked_generation = 0
        self._retry_index = False
        self._health_stale = False
        self._health_detail: str | None = None
        self._health_at_ms = 0
        self._health_hooks: list[HealthHook] = []
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        for cell_id, (cell, dirty) in cache.load().items():
            self._cells[cell_id] = cell
            if dirty:
                self._dirty.add(cell_id)
        self._index = CellIndex(1, self._cells)
        if cache.skipped_rows:
            self._log("warn", "cell.cache_rows_skipped", count=cache.skipped_rows)

    @property
    def identity(self) -> CellIdentity:
        return self._identity

    def index(self) -> CellIndex:
        with self._lock:
            return self._index

    def lookup(self, cell_id: str) -> Cell | None:
        """The held cell."""

        with self._lock:
            return self._index.get(cell_id)

    def dirty(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._dirty)

    def ensure(self, spec: CellSpec) -> Cell:
        derived = spec.binding.derive(spec.kind, self._authority)
        cell_id = derived if derived is not None else mint_cell_id()
        with self._lock:
            existing = self._cells.get(cell_id)
            if existing is not None:
                if existing.authority != self._authority:
                    raise CellError("not-authority", "cell is held by another authority")
                if existing.retired:
                    raise CellError("retired", f"{cell_id} is retired")
                return existing
            assert spec.scope is not None
            cell = Cell(
                cell_id=cell_id,
                kind=spec.kind,
                authority=self._authority,
                scope=spec.scope,
                binding=spec.binding,
                name=spec.name,
                members=tuple(spec.members),
                permissions=spec.permissions,
                params=spec.params,
                updated_at_ms=self._clock(),
            )
            self._commit_local(cell)
        self.kick()
        return cell

    def update(
        self,
        cell_id: str,
        *,
        name: str | None = None,
        members: Sequence[CellMember] | None = None,
        permissions: CellPermissions | None = None,
        params: Mapping[str, Any] | None = None,
        expect_revision: int | None = None,
    ) -> Cell:
        changes: dict[str, Any] = {}
        if name is not None:
            changes["name"] = name
        if members is not None:
            changes["members"] = tuple(members)
        if permissions is not None:
            changes["permissions"] = permissions
        if params is not None:
            changes["params"] = params
        return self._revise(cell_id, changes, expect_revision)

    def retire(self, cell_id: str, *, expect_revision: int | None = None) -> Cell:
        return self._revise(cell_id, {"retired": True}, expect_revision)

    def kick(self) -> None:
        self._wake.set()

    def request_index_retry(self) -> None:
        with self._lock:
            if self._thread is None or self._stop.is_set():
                return
            self._retry_index = True

    def on_index(self, hook: IndexHook) -> Callable[[], None]:
        with self._lock:
            self._hooks.append(hook)

        def unregister() -> None:
            with self._lock:
                if hook in self._hooks:
                    self._hooks.remove(hook)

        return unregister

    def health(self) -> CellRuntimeHealth:
        """The in-memory source health; never probes OrgFS."""

        with self._lock:
            return CellRuntimeHealth(
                source_stale=self._health_stale,
                detail=self._health_detail,
                updated_at_ms=self._health_at_ms,
            )

    def on_health(self, hook: HealthHook) -> Callable[[], None]:
        with self._lock:
            self._health_hooks.append(hook)

        def unregister() -> None:
            with self._lock:
                if hook in self._health_hooks:
                    self._health_hooks.remove(hook)

        return unregister

    def _set_health(self, *, stale: bool, detail: str | None) -> None:
        with self._lock:
            if stale == self._health_stale and detail == self._health_detail:
                return
            self._health_stale = stale
            self._health_detail = detail
            self._health_at_ms = self._clock()
            at_ms = self._health_at_ms
            hooks = tuple(self._health_hooks)
        for hook in hooks:
            try:
                hook(CellRuntimeHealth(stale, detail, at_ms))
            except Exception as error:  # noqa: BLE001 - health hooks are fenced
                self._log("warn", "cell.health_hook_failed", errorType=type(error).__name__,
                          detail=str(error)[:300])

    def _mark_source_stale(self, detail: str) -> None:
        self._set_health(stale=True, detail=detail)

    def _mark_source_fresh(self) -> None:
        self._set_health(stale=False, detail=None)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name=THREAD_NAME, daemon=True)
        self._thread.start()

    def close(self, timeout: float = 5.0) -> bool:
        """Stop the thread; True when it ended within *timeout*."""

        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                return False
        self._cache.close()
        return True

    def run_once(self) -> int:
        """One pass; returns the index generation after it."""

        with self._pass_lock:
            changed = False
            try:
                writer_of = self._pass_writer_of()
                self._flush(writer_of)
                changed = self._refresh(writer_of)
            except Exception as error:  # noqa: BLE001 - a pass never kills the runtime
                self._log("warn", "cell.pass_failed", errorType=type(error).__name__,
                          detail=str(error)[:300])
            with self._lock:
                generation = self._index.generation
                retry = self._retry_index
            if changed or generation != self._hooked_generation or retry:
                self._run_hooks(generation)
            return generation

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            self.run_once()

    def _pass_writer_of(self) -> WriterOf:
        try:
            raw = {str(key): str(value) for key, value in self._writer_bindings().items()}
        except Exception as error:  # noqa: BLE001 - a broken binding source keeps the last one
            self._log("warn", "cell.writer_bindings_failed", errorType=type(error).__name__,
                      detail=str(error)[:300])
            raw = self._bindings_snapshot
        else:
            self._bindings_snapshot = raw
        bindings = {key: value for key, value in raw.items() if key != self._authority}
        bindings[self._authority] = self._identity.writer
        if self._pass_bindings is not None and bindings != self._pass_bindings:
            self._mirror.forget_rejections()
            with self._lock:
                self._seen.clear()
            self._log("info", "cell.writer_bindings_changed", bindings=len(bindings))
        self._pass_bindings = bindings
        return bindings.get

    def _revise(self, cell_id: str, changes: dict[str, Any], expect_revision: int | None) -> Cell:
        with self._lock:
            cell = self._cells.get(cell_id)
            if cell is None:
                raise CellError("unknown-cell", f"{cell_id} is not known")
            require_authority(cell, self._authority)
            if cell.retired:
                raise CellError("retired", f"{cell_id} is retired")
            if expect_revision is not None and expect_revision != cell.revision:
                raise CellError("stale-revision", f"{cell_id} is at revision {cell.revision}")
            revised = cell.revised(now_ms=self._clock(), **changes)
            if self._same_content(cell, revised):
                return cell
            self._commit_local(revised)
        self.kick()
        return revised

    @staticmethod
    def _same_content(old: Cell, new: Cell) -> bool:
        return (old.name, old.members, old.permissions, dict(old.params), old.retired) == (
            new.name, new.members, new.permissions, dict(new.params), new.retired
        )

    def _commit_local(self, cell: Cell) -> None:
        self._cache.put(cell, dirty=True)
        self._cells[cell.cell_id] = cell
        self._dirty.add(cell.cell_id)
        self._publish()

    def _publish(self) -> None:
        self._index = CellIndex(self._index.generation + 1, self._cells)

    def _flush(self, writer_of: WriterOf) -> None:
        with self._lock:
            pending = sorted(self._dirty)
        for cell_id in pending:
            with self._lock:
                cell = self._cells.get(cell_id)
            if cell is None or cell.authority != self._authority:
                continue
            try:
                spaces = self._spaces.write_spaces(cell.scope, self._identity.writer)
            except Exception as error:  # noqa: BLE001 - spaces unreachable keeps it dirty
                self._log("warn", "cell.spaces_unavailable", cell=cell_id,
                          errorType=type(error).__name__)
                self._mark_source_stale(f"write-spaces:{type(error).__name__}")
                continue
            if not spaces:
                continue
            written = [self._write_copy(space, cell, writer_of) for space in spaces]
            if all(written):
                with self._lock:
                    if self._cells.get(cell_id) is cell and self._cache.mark_clean(
                        cell_id, cell.revision
                    ):
                        self._dirty.discard(cell_id)

    def _write_copy(self, space: SpaceRef, cell: Cell, writer_of: WriterOf) -> bool:
        copy = self._cache.copies(cell.cell_id).get(space.space_id)
        expect = copy[1] if copy is not None else None
        for _attempt in range(WRITE_ATTEMPTS):
            try:
                node_id, version = self._mirror.write(space, cell, expect)
            except OrgFsError as error:
                if error.code != "stale-write":
                    return self._write_failed(space, cell, error.code)
                try:
                    found = self._mirror.read(space, cell.kind, cell.cell_id, writer_of)
                except Exception as read_error:  # noqa: BLE001 - unreadable means retry later
                    return self._write_failed(space, cell, f"read:{type(read_error).__name__}")
                if isinstance(found, RemoteCopy) and found.cell.authority == cell.authority:
                    self._cache.set_copy(cell.cell_id, space.space_id, found.node_id, found.version)
                    if found.cell.revision >= cell.revision:
                        self._adopt_over_intent(found, cell)
                        return False
                    expect = found.version
                    continue
                if found is not None:
                    self._block(space.space_id, cell.cell_id, found)
                    return False
                self._cache.drop_copy(cell.cell_id, space.space_id)
                expect = None
                continue
            except Exception as error:  # noqa: BLE001 - OrgFS unreachable keeps it dirty
                return self._write_failed(space, cell, type(error).__name__)
            self._cache.set_copy(cell.cell_id, space.space_id, node_id, version)
            self._seen.pop((space.space_id, cell.cell_id), None)
            self._unblock(space.space_id, cell.cell_id)
            return True
        return self._write_failed(space, cell, "attempts-exhausted")

    def _block(self, space_id: str, cell_id: str, found: ReadResult) -> None:
        reason = found.reason if isinstance(found, Rejection) else "foreign-authority"
        with self._lock:
            previous = self._blocked.get((space_id, cell_id))
            self._blocked[(space_id, cell_id)] = found.version
            blocked = len(self._blocked)
        if previous != found.version:
            self._log("warn", "cell.document_blocked", cell=cell_id, space=space_id,
                      version=found.version, reason=reason, blocked=blocked)

    def _unblock(self, space_id: str, cell_id: str) -> None:
        with self._lock:
            if self._blocked.pop((space_id, cell_id), None) is None:
                return
            blocked = len(self._blocked)
        self._log("info", "cell.document_unblocked", cell=cell_id, space=space_id,
                  blocked=blocked)

    def _write_failed(self, space: SpaceRef, cell: Cell, code: str) -> bool:
        self._log("warn", "cell.write_failed", cell=cell.cell_id, space=space.space_id, code=code)
        return False

    def _adopt_over_intent(self, copy: RemoteCopy, intent: Cell) -> None:
        with self._lock:
            if self._cells.get(intent.cell_id) is intent:
                self._install(copy, dropped=intent)

    def _refresh(self, writer_of: WriterOf) -> bool:
        try:
            spaces = self._spaces.read_spaces()
        except Exception as error:  # noqa: BLE001 - OrgFS down: serve the cache
            self._log("warn", "cell.spaces_unavailable", errorType=type(error).__name__)
            self._mark_source_stale(f"read-spaces:{type(error).__name__}")
            return False
        changed = False
        scan_failed = False
        for space in spaces:
            for kind in sorted(EAGER_KINDS):
                try:
                    copies = self._mirror.scan(space, kind, writer_of, self._unchanged(space))
                except Exception as error:  # noqa: BLE001 - one space never blocks others
                    self._log("warn", "cell.scan_failed", space=space.space_id, kind=kind,
                              errorType=type(error).__name__)
                    scan_failed = True
                    continue
                for copy in copies:
                    changed = self._observe(copy) or changed
        if scan_failed:
            self._mark_source_stale("scan-failed")
        else:
            self._mark_source_fresh()
        self._requeue_missing_copies()
        return changed

    def _unchanged(self, space: SpaceRef) -> Callable[[str, str], bool]:
        def unchanged(cell_id: str, version: str) -> bool:
            if self._seen.get((space.space_id, cell_id)) == version:
                return True
            copy = self._cache.copies(cell_id).get(space.space_id)
            return copy is not None and copy[1] == version

        return unchanged

    def _observe(self, found: ReadResult) -> bool:
        if isinstance(found, Rejection):
            self._untrusted_at(found.space_id, found.cell_id, found)
            return False
        return self._merge(found)

    def _untrusted_at(self, space_id: str, cell_id: str, found: ReadResult) -> None:
        """A document at *cell_id*'s path in *space_id* that readers will not adopt."""

        version = found.version
        with self._lock:
            self._seen[(space_id, cell_id)] = version
            held = self._cells.get(cell_id)
            if held is None or held.authority != self._authority:
                return
        self._block(space_id, cell_id, found)
        with self._lock:
            if cell_id in self._dirty:
                return
            copy = self._cache.copies(cell_id).get(space_id)
            if copy is None or copy[1] == version:
                return
            self._cache.put(held, dirty=True)
            self._dirty.add(cell_id)
        self.kick()

    def _merge(self, copy: RemoteCopy) -> bool:
        cell = copy.cell
        with self._lock:
            held = self._cells.get(cell.cell_id)
            local = cell.authority == self._authority
            if held is not None and held.authority != cell.authority:
                if local:
                    self._cache.drop_copies(cell.cell_id)
                    self._cache.set_copy(cell.cell_id, copy.space_id, copy.node_id, copy.version)
                    return self._install(copy, dropped=None)
                if held.authority == self._authority:
                    self._untrusted_at(copy.space_id, cell.cell_id, copy)
                else:
                    self._seen[(copy.space_id, cell.cell_id)] = copy.version
                    self._log("warn", "cell.authority_conflict", cell=cell.cell_id,
                              space=copy.space_id)
                return False
            if held is None and not local and cell.binding.type == "subject":
                self._seen[(copy.space_id, cell.cell_id)] = copy.version
                return False
            self._cache.set_copy(cell.cell_id, copy.space_id, copy.node_id, copy.version)
            self._seen.pop((copy.space_id, cell.cell_id), None)
            if held is None:
                return self._install(copy, dropped=None)
            dirty = cell.cell_id in self._dirty
            if cell.revision < held.revision:
                return False
            if cell.revision == held.revision and (dirty or not self._outranks(cell, held)):
                return False
            return self._install(copy, dropped=held if dirty else None)

    @staticmethod
    def _outranks(candidate: Cell, held: Cell) -> bool:
        return (candidate.updated_at_ms, encode(candidate)) > (held.updated_at_ms, encode(held))

    def _install(self, copy: RemoteCopy, *, dropped: Cell | None) -> bool:
        if dropped is not None:
            self._log("info", "cell.intent_dropped", cell=dropped.cell_id,
                      intentRevision=dropped.revision, observedRevision=copy.cell.revision)
        self._cache.put(copy.cell, dirty=False)
        self._cells[copy.cell.cell_id] = copy.cell
        self._dirty.discard(copy.cell.cell_id)
        self._publish()
        return True

    def _requeue_missing_copies(self) -> None:
        with self._lock:
            candidates = [
                cell
                for cell in self._cells.values()
                if cell.authority == self._authority
                and cell.scope.kind == "org"
                and cell.scope.org is None
                and cell.cell_id not in self._dirty
            ]
        if not candidates:
            return
        try:
            spaces = self._spaces.write_spaces(candidates[0].scope, self._identity.writer)
        except Exception:  # noqa: BLE001 - next pass retries
            return
        for cell in candidates:
            copies = self._cache.copies(cell.cell_id)
            if any(space.space_id not in copies for space in spaces):
                with self._lock:
                    self._dirty.add(cell.cell_id)
                self.kick()

    def _run_hooks(self, generation: int) -> None:
        self._hooked_generation = generation
        with self._lock:
            self._retry_index = False
            hooks = tuple(self._hooks)
        for hook in hooks:
            try:
                hook(generation)
            except Exception as error:  # noqa: BLE001 - hooks are fenced
                self._log("warn", "cell.index_hook_failed", generation=generation,
                          errorType=type(error).__name__, detail=str(error)[:300])

    def _log(self, level: str, event: str, **fields: Any) -> None:
        if self._logger is None:
            return
        try:
            self._logger(level, "cells", event, **fields)
        except Exception:  # noqa: BLE001 - logging never breaks the runtime
            pass


__all__ = [
    "CellRuntime",
    "DEFAULT_INTERVAL_S",
    "THREAD_NAME",
]
