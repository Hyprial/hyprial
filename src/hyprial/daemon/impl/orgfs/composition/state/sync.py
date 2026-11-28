# Decision record (phase D, package D2, 2026-11-25):
# - Plan §1.4 / §3.3: the state runtime keeps the legacy per-space mesh
#   bookkeeping (``_meshes``, ``_mesh_creations``, the lifecycle commands) and
#   only changes the value: a ``StateSpaceSync`` over the facade's
#   ``SpaceSyncHost``, attached to the facade so publications and
#   observations go to it.  ``RuntimeDirectory`` calls ``schedule_sync_from``
#   and ``announce`` on it unchanged.
# - Design §3.5 as ruled after F1 (node gate ruling): the node gate is
#   presence (checked by the sync itself) and, unless
#   ``trust_tailnet_members`` (read at every call), ``principal(node)`` being
#   a current member of the space.  ``principal(node)`` is resolved the same
#   way for every space, plain or org: the device-owner index (live rows and
#   trash history, ``_device_owner_index``) of every org directory space this
#   node holds, plus the space itself when it is an org directory still being
#   joined, together with the assembly's ``device_owner_hint`` (the bootstrap
#   source; a revoked device has no principal there).  Two directories or the
#   two sources naming different principals refuse the node and log
#   ``supplier-owner-conflict``.  A failing hint or index refuses the node and
#   logs ``supplier-owner-unresolved``, as the mesh did.
# - The device-owner index of a directory space is keyed by the space's meta
#   and tree vectors and rebuilt when either moves: the index is a function of
#   the tree's ``directory/devices`` rows, the trash and the member table, so
#   this invalidates it after every accepted update that can change it (the
#   ruling's "any accepted update"); a content-only update cannot.  The
#   rebuild drops the cache entry of ``RuntimeDirectory._device_owner_index``
#   and its meta watch first, so that builder never stacks watches.
# - An abandoned join forgets the device index it may have cached for the
#   temporary space.
# - ``_maintain_spaces`` (the due anti-entropy tick of ``RuntimeDirectory``)
#   calls each sync's ``maintain(now)``, verifies one batch
#   through ``SyncHost.pending_content`` and schedules one pull from an online
#   holder when content is pending.
# - F1 round 2 (ruling on F1 item 4): the verification backlog does not wait
#   for due ticks.  ``anti_entropy_tick``, which the maintenance thread calls
#   every pass, first verifies each space's unverified documents in
#   VERIFY_BATCH batches until none is left; each batch is one
#   ``pending_content`` call, which takes and releases the space lock, and a
#   batch that verifies nothing stops the loop.  The due tick also schedules
#   one pull when no current observation covers the local meta and tree
#   vectors (only vectors are compared; no timer), so a local write turns
#   ``synced`` true again within one due tick once a holder has it.  With no
#   holder online nothing is scheduled and ``synced`` stays ``None``.
# - F4 (real-node finding R2-1): a round whose tree came as a temporary blob
#   that failed to arrive still records the holder's observation, and that
#   observation covers the local vectors, so the rule above never pulls
#   again and nothing is pending: a joined member kept an empty tree until
#   the holder wrote.  The due tick therefore also pulls from an online holder
#   whose observed meta or tree the local vectors do not cover.  Those pulls
#   back off per space: the first is immediate, then CATALOG_RETRY_BASE_SECONDS
#   doubling up to CATALOG_RETRY_MAX_SECONDS, measured in the tick's ``now``;
#   the backoff is forgotten once no online observation is ahead.  A round
#   that fails outright records no observation and stays under the rule above
#   and the shared supplier hold, which already backs off failures.
# - Package F6 (real-node finding R3-6): a space whose local author is not a
#   current member (``SyncHost.local_member`` false) only ever holds meta, so
#   the due tick compares the meta vector alone: it pulls when no current
#   observation covers the local meta or an online holder's observed meta is
#   ahead, and never for pending content or a tree it will not receive.  A
#   space this node was removed from therefore stops being "behind" after one
#   meta-only round and no longer takes the shared supplier's single probe.
# - Package F7 (real-node finding R4-3, rulings 1 and 4): a node can hold
#   updates no other node has (their broadcast was lost, or refused then and
#   acceptable now).  The supplier side of a pull already pulls back from a
#   requester that is ahead (``sync/pull.py``), so the due tick makes the
#   node that is ahead the requester of every peer that may lack them: a
#   "lagging" holder is an online node that announced the space, or that the
#   sync knows holds it (``member_peers``: synced with, broadcast from or
#   observed), and passes the node gate, and has no observation covering
#   the local meta and tree vectors.  Lagging and ahead holders are pulled
#   together under the same per-space backoff as the F4 catalog retry (first
#   pull immediate, then CATALOG_RETRY_BASE_SECONDS doubling to
#   CATALOG_RETRY_MAX_SECONDS, forgotten once no holder is ahead or
#   lagging); the "behind" pull above keeps its own rule, now through the
#   node gate, and a lagging holder it already pulled is not pulled twice.
#   Merely online hosts that never announced the space are not lagging
#   holders, since a node that does not hold the space never answers.
#   A space whose author is not a current member has no lagging holders.
#   Storm bound: without new writes, a pair whose observations cover each
#   other schedules nothing; a pair that cannot converge (an update one side
#   keeps refusing) pulls at the backoff rate, and the supplier's pull back
#   fires once per distinct requester vectors.
# - Design §9 R5 join: a temporary space (``facade.begin_join``) is filled by
#   a temporary ``StateSpaceSync`` whose node gate is presence only, from each
#   candidate holder in turn; the space is kept only when its meta names an
#   owner, this node's author is a member and the holder passes the formal
#   node gate.  Otherwise the temporary sync is closed first, then the space
#   and its directory are dropped.  Error codes match the legacy join: no
#   candidate is ``no-holder-online``; a holder that is offline when its pull
#   fails is ``not-a-member`` with ``reason: supplier-not-online`` (the
#   retryable shape ``OrgNetworkService`` waits for); otherwise the sync's
#   code (a supplier refusing an uninvited requester answers ``not-a-member``).
#   With several holders the join raises the most informative failure, not
#   the last one (V defect 4): a node-gate or membership refusal from a
#   holder that answered, then any other sync error, then an offline holder,
#   then "no holder"; the first of the best rank wins.
#   Every join error carries a ``message`` in its details, so ``str()`` names
#   the code once and then says what happened (T2 D-1).
#   A space id that is not a UUID can never be stored and is ``unknown-space``
#   before anything is opened.
# - F1 item 3: between ``begin_join`` and ``complete_join`` the space
#   directory holds ``JOIN_MARKER`` (written and fsynced before the temporary
#   sync pulls anything, removed and fsynced once the join completed), so a
#   crash in between leaves a directory the startup scan discards instead of
#   a space that skipped the member and node-gate checks.
# - Package S: the journal-only runtime calls (``broadcast_pending``,
#   ``broadcast_records``, retirement and replica reconciliation) are deleted
#   together with the journal facade that made them.
# - Package F8 (real-node finding R5-1): a join has two outcomes only.
#   Joined: listed, ``join.pending`` removed, the resident sync running and
#   pulling.  Not joined: not listed, no directory, and the next join is a
#   first join.  The temporary sync's close (which discards its queued
#   broadcasts, ``sync/space.py``) can still fail on an apply or handler
#   that outlives its budget; that failure is logged
#   (``orgfs.join.temporary-close-failed``) and never changes the outcome,
#   since every broadcast it dropped is recovered by the resident sync's
#   pull.
# - Package F8b (review of F8, items 1, 2, 4, 5, 8, 11), revising F8's
#   rollback: a listed space is writable, so nothing ever drops it.  The
#   join removes ``join.pending`` (fsynced) inside ``complete_join`` after
#   the owner and member checks and before the listing (``confirm``).  Every
#   failure before the listing (marker write, pull, checks, marker removal)
#   discards a space no caller could write to; one that is not an
#   ``OrgFsError`` is ``unavailable`` / ``join-incomplete`` with a message.
#   Every failure after the listing (resident sync, pull scheduling, daemon
#   closing) keeps the space listed and is raised as ``unavailable``
#   (``orgfs-closing`` only when the runtime is closing, else
#   ``join-incomplete``); the next join, or any ``_mesh`` call, finishes it.
#   So ruling 1 reads: failed and not listed, or listed and a repeated join
#   reaches "syncing".  A crash after the listing finds no marker, so the
#   startup scan keeps the space and its writes.
#   The discard of an unlisted space never raises and never replaces the
#   caller's error: it first writes the marker back (so any step that then
#   fails leaves a directory the startup scan discards), waits for an
#   in-flight resident sync creation, then forgets the space and closes any
#   resident sync registered for it; each failed step is logged
#   (``orgfs.join.discard-failed``).  A join that failed for a reason other
#   than an ``OrgFsError`` logs ``orgfs.join.rolled-back``.  Messages carry
#   an ``OSError``'s type and errno text, never its path.  The announce that
#   ends a join is best effort (``_announce_all`` logs a failure).
# - Joins of one space: the lifecycle runs one at a time and later ones wait
#   for its outcome (``composition/lifecycle.py``), so they hold no worker.
#   A per-space lock still serializes the join body for an inline join; it
#   is taken only for a valid space id and its entry goes with its last user.
#   Under the lock a listed space with an owner is finished, not skipped:
#   a leftover ``join.pending`` is removed, a missing resident sync is
#   created and pulls from the online holders, so a repeated join repairs a
#   space a failed tail left behind without a restart.  A space still hidden
#   as joining while no join holds the lock is a leftover temporary space:
#   it is dropped (``orgfs.join.leftover-discarded``) and joined afresh.
#   A runtime with no transport bound (no resident sync can exist) simply
#   returns the listed space, as the facade did before.
#   ``_join_shortcut`` (asked by ``join``) answers a finished space (listed, resident sync, no marker)
#   without queueing on the lifecycle when no join of it is running.
# - Package F8c (review of F8b items 1-3 and 8):
#   A join first clears what an unfinished join left: a hidden space in
#   memory, and a directory not open in memory that holds ``join.pending`` or
#   no ``space.json`` (the startup scan's discard rule; a refused space is
#   never touched).  A discard whose directory removal failed leaves exactly
#   such a directory, and opening it as the new temporary space would skip
#   the member table and tree that ``load_space`` materializes.  When the
#   leftover cannot be removed the join stops with ``join-incomplete``.
#   ``begin_join`` runs inside the join's error handling: an ``OSError``
#   while it creates the directory is ``join-incomplete`` with the errno
#   text, and a directory it created is marked and removed (each failed step
#   logged).  A ``StoreError`` from ``begin_join`` (a refused layout) is
#   raised as it is and its directory is never removed.
#   The due maintenance tick starts the resident sync of every listed space
#   that has none (skipping spaces being purged or joined), so a space a
#   failed tail left listed syncs again without another join.
#   The join shortcut does not answer for a space being purged.
"""Per-space sync assembly, node gate and join of the state runtime."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from hyprial.daemon.impl.orgfs.api import OrgFsError, SpaceInfo
from hyprial.daemon.impl.orgfs.composition.state.stores import JOIN_MARKER
from hyprial.daemon.impl.orgfs.composition.vocabulary import _SpaceResourceHandle
from hyprial.daemon.impl.orgfs.state import is_space_directory_name, space_directory_kind
from hyprial.daemon.impl.orgfs.storage.vocabulary import StoreError
from hyprial.daemon.impl.orgfs.sync import SyncError, covers
from hyprial.daemon.impl.orgfs.sync.interface import CATALOG_DOC_IDS
from hyprial.daemon.impl.orgfs.sync.space import StateSpaceSync
from hyprial.daemon.impl.transport import KeySpace
from hyprial.identity import directory_owner_principal, is_org_directory_space
from hyprial.kernel import atomic_json_write, fsync_directory

VERIFY_BATCH = 256
JOIN_ROUND_TIMEOUT_SECONDS = 10.0
CATALOG_RETRY_BASE_SECONDS = 60.0
CATALOG_RETRY_MAX_SECONDS = 960.0
JOIN_DISCARD_WAIT_SECONDS = 5.0


@dataclass(slots=True)
class _JoinLock:
    lock: Lock = field(default_factory=Lock)
    users: int = 0


def _failure_detail(exc: BaseException) -> str:
    """What a wire error may say about ``exc``: no local path (F8b item 11)."""

    if isinstance(exc, OSError):
        reason = os.strerror(exc.errno) if isinstance(exc.errno, int) else ""
        return f"{type(exc).__name__}: {reason}" if reason else type(exc).__name__
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _join_incomplete(space_id: str, message: str) -> OrgFsError:
    return OrgFsError(
        "unavailable", {"spaceId": space_id, "reason": "join-incomplete", "message": message}
    )


def _catalog_vectors(vectors: Any, catalog: tuple[str, ...]) -> tuple[bytes, ...]:
    return tuple(vectors.get(doc_id) or b"\x00" for doc_id in catalog)


def _observed_vectors(item: Any, catalog: tuple[str, ...]) -> tuple[bytes, ...]:
    values = {"meta": item.meta_vector, "tree": item.tree_vector}
    return tuple(values[doc_id] for doc_id in catalog)


def _join_error_rank(error: OrgFsError) -> int:
    """V defect 4: how informative a failed holder's error is for the caller."""

    reason = error.details.get("reason") if isinstance(error.details, dict) else None
    if error.code == "no-holder-online":
        return 0
    if reason == "supplier-not-online":
        return 1
    if error.code == "not-a-member":
        return 3
    return 2


class StateLifecycle:
    """Sync assembly and join of ``OrgFsRuntime``."""

    def _log(self, level: str, event: str, **fields: object) -> None:
        if self.logger is None:
            return
        try:
            self.logger(level, event, **fields)
        except Exception:
            return

    def _node_gate(self, space_id: str) -> Callable[[str], bool]:
        """Design §3.5: the device of ``node`` belongs to a current member."""

        resolver = self._device_owner_resolver(space_id)

        def gate(node: str) -> bool:
            if self._trust_tailnet_members:
                return True
            try:
                principal = resolver(node)
            except Exception as exc:
                self._log(
                    "warn",
                    "orgfs.update.rejected",
                    reason="supplier-owner-unresolved",
                    supplier=node,
                    spaceId=space_id,
                    detail=str(exc),
                )
                return False
            if principal is None:
                return False
            space = self.facade._existing_space(space_id)
            return space is not None and principal in space.members

        return gate

    def bind_device_owner_hint(self, hint: Callable[[str], str | None] | None) -> None:
        """Set the assembly's bootstrap ``node -> principal`` source."""

        self._device_owner_hint = hint

    def _device_owner_resolver(self, space_id: str) -> Callable[[str], str | None]:
        """``node -> principal`` from every org directory held here and the hint."""

        def resolve(node: str) -> str | None:
            if not node:
                return None
            found: dict[str, str] = {}
            for directory in self._directory_spaces(space_id):
                principal = self._device_owner_index(directory).get(node)
                if principal:
                    found.setdefault(principal, f"directory:{directory}")
            hint = self._device_owner_hint
            hinted = hint(node) if hint is not None else None
            if hinted:
                found.setdefault(directory_owner_principal(hinted), "hint")
            if len(found) > 1:
                self._log(
                    "warn",
                    "orgfs.update.rejected",
                    reason="supplier-owner-conflict",
                    supplier=node,
                    spaceId=space_id,
                    principals=sorted(found),
                    sources=sorted(found.values()),
                )
                return None
            return next(iter(found), None)

        return resolve

    def _directory_spaces(self, space_id: str) -> tuple[str, ...]:
        directories = [
            info.space_id for info in self.facade.spaces() if is_org_directory_space(info.name)
        ]
        space = self.facade._existing_space(space_id)
        if space is not None and is_org_directory_space(space.info.name) and space_id not in directories:
            directories.append(space_id)
        return tuple(directories)

    def _device_owner_index(self, directory_space: str) -> dict[str, str]:
        key = self._directory_index_key(directory_space)
        with self._supplier_device_lock:
            current = self._directory_index_keys.get(directory_space) == key
            cached = self._supplier_device_owners.get(directory_space) if current else None
        if cached is not None:
            return cached
        self._forget_device_index(directory_space)
        index = super()._device_owner_index(directory_space)
        with self._supplier_device_lock:
            self._directory_index_keys[directory_space] = key
        return index

    def _directory_index_key(self, directory_space: str) -> tuple[bytes, bytes]:
        vectors = self.facade.sync_host(directory_space).read_vectors(CATALOG_DOC_IDS)
        return bytes(vectors.get("meta") or b""), bytes(vectors.get("tree") or b"")

    def _forget_device_index(self, space_id: str) -> None:
        with self._supplier_device_lock:
            self._directory_index_keys.pop(space_id, None)
            watch = self._supplier_device_watches.pop(space_id, None)
            self._supplier_device_owners.pop(space_id, None)
        close = getattr(watch, "close", None)
        if callable(close):
            close()

    def _holder_discovery(self, space_id: str) -> Callable[[], tuple[str, ...]]:
        def discover() -> tuple[str, ...]:
            return tuple(
                dict.fromkeys((*self._holder_snapshot(space_id), *self._holder_candidates()))
            )

        return discover

    def _new_sync(self, space_id: str, gate: Callable[[str], bool], *, node_level: bool) -> StateSpaceSync:
        with self._resource_lock:
            session = self._session
        if session is None:
            raise OrgFsError("unavailable", {"message": "orgfs transport is not bound"})
        return StateSpaceSync(
            session,
            KeySpace(),
            space_id,
            self.node_id,
            self.author,
            self.facade.sync_host(space_id, gate),
            self.blobs,
            self._supplier_sync_health,
            self._supplier_online,
            self.logger,
            announcement_source=self._announcement_spaces if node_level else None,
            holder_discovery=self._holder_discovery(space_id),
        )

    def _create_mesh_sync(self, space_id: str) -> _SpaceResourceHandle | None:
        with self._resource_lock:
            session = self._session
            if self._closing or session is None:
                return None
            existing = self._meshes.get(space_id)
        if existing is not None:
            return _SpaceResourceHandle(space_id, self._directory_generation)
        if space_id not in {info.space_id for info in self.facade.spaces()}:
            return None
        sync = self._new_sync(space_id, self._node_gate(space_id), node_level=True)
        with self._resource_lock:
            if self._closing or self._session is not session:
                current = None
                discard = True
            else:
                current = self._meshes.get(space_id)
                if current is None:
                    current = sync
                    self._meshes[space_id] = sync
                discard = current is not sync
        if discard:
            sync.close()
            return (
                _SpaceResourceHandle(space_id, self._directory_generation)
                if current is not None
                else None
            )
        self.facade.attach_sync(space_id, sync)
        self.facade.request_broadcast_drain(space_id)
        self._announce_all()
        return _SpaceResourceHandle(space_id, self._directory_generation)

    def anti_entropy_tick(self, now: float) -> int:
        """Drain every verification backlog, then the due-gated tick (F1 round 2)."""

        try:
            self._drain_verification()
        except Exception as exc:  # noqa: BLE001 - must not skip later maintenance
            self._log("warn", "orgfs.anti_entropy.failed", error=repr(exc))
        return super().anti_entropy_tick(now)

    def _drain_verification(self) -> int:
        """Verify unverified documents in VERIFY_BATCH batches until none is left."""

        batches = 0
        for info in self.facade.spaces():
            registry = self.facade._registry(info.space_id)
            host = self.facade.sync_host(info.space_id)
            left = len(registry.unverified)
            while left:
                host.pending_content(VERIFY_BATCH)
                batches += 1
                now_left = len(registry.unverified)
                if now_left >= left:
                    break
                left = now_left
        return batches

    def _start_missing_syncs(self) -> None:
        """Start the resident sync of a listed space that has none (F8c item 3)."""

        with self._resource_lock:
            if self._closing or self._session is None:
                return
            skipped = {*self._meshes, *self._purging_spaces}
        with self._join_locks_guard:
            skipped.update(self._join_locks)
        for info in self.facade.spaces():
            if info.space_id in skipped:
                continue
            try:
                self._mesh(info.space_id)
            except Exception as exc:
                self._log("warn", "orgfs.sync.maintain-failed", spaceId=info.space_id,
                          detail=_failure_detail(exc))

    def _maintain_spaces(self, now: float) -> None:
        """Hook: ``SpaceSync.maintain``, verification, and a pull when behind."""

        self._start_missing_syncs()
        with self._resource_lock:
            syncs = tuple(self._meshes.items())
        for space_id, sync in syncs:
            try:
                sync.maintain(now)
                host = self.facade.sync_host(space_id)
                catalog = CATALOG_DOC_IDS if host.local_member() else ("meta",)
                pending = host.pending_content(VERIFY_BATCH) if len(catalog) > 1 else ()
                behind = bool(pending) or not self._observed(host, sync, catalog)
                ahead = self._ahead_holders(host, sync, catalog)
                lagging = self._lagging_holders(space_id, host, sync) if len(catalog) > 1 else []
                gate = self._node_gate(space_id)
            except Exception as exc:
                self._log("warn", "orgfs.sync.maintain-failed", spaceId=space_id, detail=str(exc))
                continue
            if not ahead and not lagging:
                self._catalog_retries.pop(space_id, None)
            pulled: list[str] = []
            if behind:
                pulled = [
                    node
                    for node in self._holder_discovery(space_id)()
                    if node != self.node_id and self._supplier_online(node) and gate(node)
                ][:1]
                for node in pulled:
                    sync.schedule_sync_from(node)
            targets = [node for node in dict.fromkeys([*ahead, *lagging]) if node not in pulled]
            if targets and self._catalog_retry_due(space_id, targets[0], now):
                for node in targets:
                    sync.schedule_sync_from(node)

    @staticmethod
    def _observed(host: Any, sync: Any, catalog: tuple[str, ...] = CATALOG_DOC_IDS) -> bool:
        """Whether a current observation covers the local ``catalog`` vectors."""

        local = _catalog_vectors(host.read_vectors(catalog), catalog)
        return any(
            all(covers(seen, mine) for seen, mine in zip(_observed_vectors(item, catalog), local))
            for item in sync.observations()
        )

    def _ahead_holders(
        self, host: Any, sync: Any, catalog: tuple[str, ...] = CATALOG_DOC_IDS
    ) -> list[str]:
        """Online holders with an observed ``catalog`` vector the local ones do not cover."""

        local = _catalog_vectors(host.read_vectors(catalog), catalog)
        return sorted(
            item.node
            for item in sync.observations()
            if item.node != self.node_id
            and self._supplier_online(item.node)
            and not all(covers(mine, seen) for seen, mine in zip(_observed_vectors(item, catalog), local))
        )

    def _lagging_holders(self, space_id: str, host: Any, sync: Any) -> list[str]:
        """Online member holders with no observation covering the local meta and tree."""

        local = _catalog_vectors(host.read_vectors(CATALOG_DOC_IDS), CATALOG_DOC_IDS)
        seen = {item.node: item for item in sync.observations()}
        gate = self._node_gate(space_id)
        known = getattr(sync, "member_peers", tuple)()
        return [
            node
            for node in dict.fromkeys([*self._holder_snapshot(space_id), *known, *sorted(seen)])
            if node != self.node_id
            and self._supplier_online(node)
            and gate(node)
            and (
                node not in seen
                or not all(covers(theirs, mine) for theirs, mine in
                           zip(_observed_vectors(seen[node], CATALOG_DOC_IDS), local))
            )
        ]

    def _catalog_retry_due(self, space_id: str, holder: str, now: float) -> bool:
        """Back off pulls for a catalog an online holder is ahead of (F4, R2-1)."""

        attempts, next_at = self._catalog_retries.get(space_id, (0, now))
        if now < next_at:
            return False
        attempts += 1
        delay = min(CATALOG_RETRY_BASE_SECONDS * 2 ** (attempts - 1), CATALOG_RETRY_MAX_SECONDS)
        self._catalog_retries[space_id] = (attempts, now + delay)
        self._log(
            "info",
            "orgfs.sync.catalog-behind",
            spaceId=space_id,
            supplier=holder,
            attempt=attempts,
            nextRetrySeconds=delay,
        )
        return True

    @contextmanager
    def _join_guard(self, space_id: str, *, blocking: bool = True) -> Iterator[bool]:
        """Hold the join lock of ``space_id``; its entry goes with its last user."""

        with self._join_locks_guard:
            entry = self._join_locks.setdefault(space_id, _JoinLock())
            entry.users += 1
        held = entry.lock.acquire(blocking)
        try:
            yield held
        finally:
            if held:
                entry.lock.release()
            with self._join_locks_guard:
                entry.users -= 1
                if entry.users == 0 and self._join_locks.get(space_id) is entry:
                    del self._join_locks[space_id]

    @staticmethod
    def _require_space_id(space_id: str) -> None:
        if not isinstance(space_id, str) or not is_space_directory_name(space_id):
            raise OrgFsError("unknown-space", {"spaceId": str(space_id), "reason": "not-a-uuid"})

    def _join_marker(self, space_id: str) -> Path:
        return self._spaces_root / space_id / JOIN_MARKER

    def _write_join_marker(self, space_id: str) -> None:
        marker = self._join_marker(space_id)
        atomic_json_write(marker, {"spaceId": space_id})
        fsync_directory(marker.parent)

    def _restore_join_marker(self, space_id: str) -> None:
        """Mark a directory being discarded so a restart discards it too."""

        marker = self._join_marker(space_id)
        if marker.parent.is_dir() and not marker.exists():
            self._write_join_marker(space_id)

    def _drop_join_marker(self, space_id: str) -> None:
        marker = self._join_marker(space_id)
        if marker.exists():
            marker.unlink()
            fsync_directory(marker.parent)

    def _joined_info(self, space_id: str) -> SpaceInfo | None:
        """A listed space whose join finished: no marker and a resident sync."""

        if space_id not in {info.space_id for info in self.facade.spaces()}:
            return None
        existing = self.facade._existing_space(space_id)
        if existing is None or not existing.info.owner:
            return None
        with self._resource_lock:
            resident = space_id in self._meshes or self._session is None
            purging = space_id in self._purging_spaces
        if purging or not resident or self._join_marker(space_id).exists():
            return None
        return existing.info

    def _join_shortcut(self, space_id: str) -> SpaceInfo | None:
        self._require_space_id(space_id)
        with self._join_guard(space_id, blocking=False) as held:
            return self._joined_info(space_id) if held else None

    def _join_sync(self, space_id: str) -> SpaceInfo:
        self._require_space_id(space_id)
        with self._join_guard(space_id):
            return self._join_locked(space_id)

    def _join_locked(self, space_id: str) -> SpaceInfo:
        existing = self.facade._existing_space(space_id)
        if existing is not None:
            listed = space_id in {info.space_id for info in self.facade.spaces()}
            if listed and existing.info.owner:
                return self._finish_join(space_id, existing.info, None)
        self._clear_leftover(space_id)
        holders = sorted(
            {
                *(node for node in self._holder_snapshot(space_id) if node != self.node_id),
                *(node for node in self._holder_candidates() if node != self.node_id),
            }
        )
        if not holders:
            raise OrgFsError("no-holder-online", {"spaceId": space_id})
        created = not (self._spaces_root / space_id).exists()
        begun = False
        try:
            self.facade.begin_join(space_id)
            begun = True
            self._write_join_marker(space_id)
            info, holder = self._fill_joined_space(space_id, holders)
        except BaseException as exc:
            refused = isinstance(exc, OrgFsError) or not isinstance(exc, Exception)
            if begun:
                self._discard_temporary(space_id, None if refused else "orgfs.join.rolled-back")
            else:
                refused = refused or isinstance(exc, StoreError)
                if created:
                    self._discard_unbegun(space_id)
            if refused:
                raise
            raise _join_incomplete(
                space_id, f"the join could not finish and nothing was joined: {_failure_detail(exc)}"
            ) from exc
        return self._finish_join(space_id, info, holder)

    def _leftover_space(self, space_id: str) -> bool:
        if self.facade._existing_space(space_id) is None:
            return False
        return space_id not in {info.space_id for info in self.facade.spaces()}

    def _leftover_directory(self, space_id: str) -> bool:
        directory = self._spaces_root / space_id
        if self.facade._existing_space(space_id) is not None or not directory.is_dir():
            return False
        if space_id in self.facade._refused_spaces:
            return False
        return (directory / JOIN_MARKER).exists() or space_directory_kind(directory) == "uninitialized"

    def _clear_leftover(self, space_id: str) -> None:
        """Drop what an unfinished join left in memory or on disk (F8c item 1)."""

        if self._leftover_space(space_id):
            self._discard_temporary(space_id, "orgfs.join.leftover-discarded")
        try:
            if self._leftover_directory(space_id):
                self._forget_device_index(space_id)
                self._discard_space_directory(space_id)
                self._log("warn", "orgfs.join.leftover-discarded", spaceId=space_id)
            left = self._leftover_space(space_id) or self._leftover_directory(space_id)
        except Exception as exc:
            raise _join_incomplete(
                space_id,
                "an earlier join left files that could not be removed "
                f"({_failure_detail(exc)}); nothing was joined",
            ) from exc
        if left:
            raise _join_incomplete(
                space_id, "an earlier join left a space that could not be removed; nothing was joined"
            )

    def _discard_unbegun(self, space_id: str) -> None:
        """Remove the directory a failed ``begin_join`` created; never raises (F8c item 2)."""

        steps: tuple[tuple[str, Callable[[], object]], ...] = (
            ("marker", lambda: self._restore_join_marker(space_id)),
            ("directory", lambda: self._discard_space_directory(space_id)),
        )
        for step, action in steps:
            try:
                action()
            except Exception as exc:
                self._log("warn", "orgfs.join.discard-failed", spaceId=space_id, step=step,
                          detail=_failure_detail(exc))
        self._log("warn", "orgfs.join.rolled-back", spaceId=space_id)

    def _finish_join(self, space_id: str, info: SpaceInfo, holder: str | None) -> SpaceInfo:
        """Start the resident sync of a listed space and pull (F8, F8b).

        The space stays listed whatever fails here, since it may hold local
        writes; the failure is raised and the next join finishes the space.
        """

        try:
            self._drop_join_marker(space_id)
            sync = self._mesh(space_id)
            if sync is not None:
                gate = self._node_gate(space_id)
                sources = [holder] if holder is not None else [
                    node for node in self._holder_discovery(space_id)()
                    if node != self.node_id and self._supplier_online(node) and gate(node)
                ]
                for node in sources:
                    sync.schedule_sync_from(node)
        except OrgFsError:
            raise
        except Exception as exc:
            raise OrgFsError(
                "unavailable",
                {"spaceId": space_id, "reason": "join-incomplete",
                 "message": f"the space is joined but its sync did not start ({_failure_detail(exc)}); "
                            "join again to finish"},
            ) from exc
        if sync is None:
            with self._resource_lock:
                closing, bound = self._closing, self._session is not None
            if closing:
                raise OrgFsError(
                    "unavailable",
                    {"spaceId": space_id, "reason": "orgfs-closing",
                     "message": "orgfs is closing; the space is joined and syncs after the next start"},
                )
            if bound:
                raise OrgFsError(
                    "unavailable",
                    {"spaceId": space_id, "reason": "join-incomplete",
                     "message": "the space is joined but its sync did not start; join again to finish"},
                )
            return info
        self._announce_all()
        return info

    def _discard_temporary(self, space_id: str, event: str | None) -> None:
        """Drop a join space that was never listed; never raises (F8b items 5, 8)."""

        def await_creation() -> None:
            with self._resource_lock:
                creation = self._mesh_creations.get(space_id)
            if creation is not None:
                creation.done.wait(JOIN_DISCARD_WAIT_SECONDS)

        def close_resident() -> None:
            with self._resource_lock:
                mesh = self._meshes.pop(space_id, None)
            if mesh is not None:
                self.facade.detach_sync(space_id)
                mesh.close()

        steps: tuple[tuple[str, Callable[[], object]], ...] = (
            ("marker", lambda: self._restore_join_marker(space_id)),
            ("sync-creation", await_creation),
            ("device-index", lambda: self._forget_device_index(space_id)),
            ("space", lambda: self.facade.abandon_join(space_id)),
            ("resident-sync", close_resident),
        )
        for step, action in steps:
            try:
                action()
            except Exception as exc:
                self._log("warn", "orgfs.join.discard-failed", spaceId=space_id, step=step,
                          detail=_failure_detail(exc))
        if event is not None:
            self._log("warn", event, spaceId=space_id)

    def _close_temporary(self, space_id: str, temporary: StateSpaceSync) -> None:
        try:
            temporary.close()
        except Exception as exc:  # noqa: BLE001 - F8 ruling 2: the join outcome stands
            self._log("warn", "orgfs.join.temporary-close-failed", spaceId=space_id, detail=str(exc))

    def _fill_joined_space(self, space_id: str, holders: list[str]) -> tuple[SpaceInfo, str]:
        temporary = self._new_sync(space_id, lambda _node: True, node_level=False)
        result: tuple[SpaceInfo, str] | None = None
        best: OrgFsError = OrgFsError(
            "no-holder-online", {"spaceId": space_id, "message": "no holder of the space is online"}
        )

        def keep(error: OrgFsError) -> None:
            nonlocal best
            if _join_error_rank(error) > _join_error_rank(best):
                best = error

        try:
            gate = self._node_gate(space_id)
            for holder in holders:
                try:
                    temporary.sync_from(holder, timeout=JOIN_ROUND_TIMEOUT_SECONDS)
                except SyncError as exc:
                    keep(self._join_error(space_id, holder, exc.code, exc.details, str(exc)))
                    continue
                if not self._supplier_online(holder):
                    keep(self._join_error(space_id, holder, "not-a-member", {}, "supplier went offline"))
                    continue
                if not gate(holder):
                    keep(OrgFsError(
                        "not-a-member",
                        {"spaceId": space_id, "supplier": holder, "reason": "supplier-not-member",
                         "message": "supplier's device owner is not a current member"},
                    ))
                    continue
                try:
                    info = self.facade.complete_join(
                        space_id, confirm=lambda: self._drop_join_marker(space_id)
                    )
                    result = (info, holder)
                except OrgFsError as exc:
                    keep(exc)
                    continue
                break
        finally:
            self._close_temporary(space_id, temporary)
        if result is None:
            raise best
        return result

    def _join_error(
        self, space_id: str, holder: str, code: str, details: Any, message: str
    ) -> OrgFsError:
        if not self._supplier_online(holder):
            return OrgFsError(
                "not-a-member",
                {"spaceId": space_id, "supplier": holder, "reason": "supplier-not-online",
                 "message": message or "supplier is not an online host"},
            )
        values = {str(key): value for key, value in dict(details or {}).items()}
        return OrgFsError(code, {**values, "spaceId": space_id, "supplier": holder, "message": message})


__all__ = [
    "CATALOG_RETRY_BASE_SECONDS",
    "CATALOG_RETRY_MAX_SECONDS",
    "JOIN_ROUND_TIMEOUT_SECONDS",
    "StateLifecycle",
    "VERIFY_BATCH",
]
