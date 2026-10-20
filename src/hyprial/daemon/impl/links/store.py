"""Local copy of the ``link/`` orgfs directory plus the service's outer ports.

Spec: docs/design/send-links-impl/wp2.md §1.1–§1.2.  ``LinkStore`` reads the
node's local orgfs copy only, picks the highest-revision complete snapshot per
actor (B12), refuses to write anything but this node's own actor files (B13)
and publishes an immutable :class:`LinkIndex` that readers may take without a
lock.

The file also defines the dataclasses and protocols by which ``service.py``
learns facts it must not query itself (inbox ledger, presence, PAC lifecycle).
Every implementation of those protocols lives in composition; the store and
the service never import inbox, dispatch, application or PAC.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol

from hyprial.daemon.impl.links.model import (
    LinkFile,
    from_document,
    link_file_path,
    to_document,
)
from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError

__all__ = [
    "CensoredRecord",
    "HeldRow",
    "InboundRow",
    "LedgerResult",
    "LedgerRow",
    "LedgerUpdate",
    "LinkIndex",
    "LinkInboxPort",
    "LinkPacPort",
    "LinkPresencePort",
    "LinkStore",
]

#: orgfs codes meaning the space is not ours to write (B13).
_MEMBERSHIP_CODES = frozenset({"not-a-member", "closed"})
#: orgfs codes meaning the directory node is absent (nothing to read).
_ABSENT_CODES = frozenset(
    {"unknown-doc", "unknown-space", "content_pending", "ORGFS_CONTENT_PENDING"}
)


@dataclass(frozen=True, slots=True)
class LinkIndex:
    """Immutable committed view, swapped atomically; read without a lock."""

    spaces: frozenset[str] = frozenset()
    writable: frozenset[str] = frozenset()
    files: Mapping[str, LinkFile] = field(default_factory=dict)
    revisions: Mapping[str, int] = field(default_factory=dict)
    absent: frozenset[str] = frozenset()
    conflicted: frozenset[str] = frozenset()
    #: Canonical ``user:`` principals that own or are members of a live link
    #: space.  Filled only when the store was given ``members_of`` (the
    #: ``HYPRIAL_LINKS_BYPASS`` ingress check); empty otherwise.
    members: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))
        object.__setattr__(self, "revisions", MappingProxyType(dict(self.revisions)))

    def known(self, actor: str) -> bool:
        """Committed, absent or conflicted all count as "seen" (B11)."""

        return actor in self.files or actor in self.absent or actor in self.conflicted

    @property
    def org_available(self) -> bool:
        return bool(self.writable)


@dataclass(frozen=True, slots=True)
class HeldRow:
    """One durable held outbox row (§1.2, B15)."""

    message_id: str
    sender: str
    target: str
    intent: str
    reply_to: str | None
    hold_reason: str
    created_at_ms: int
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class InboundRow:
    """One live inbound ledger row, injected or not (§1.2)."""

    message_id: str
    sender: str
    recipient: str
    intent: str
    reply_to: str | None
    received_at_ms: int
    fetched_at_ms: int | None = None
    injected: bool = False


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One durable ledger row (§3.9)."""

    actor: str
    message_id: str
    direction: Literal["in", "out"]
    peer: str
    intent: str
    reply_to: str | None
    state: str
    opened_at_ms: int
    sent_at_ms: int | None = None
    event_at_ms: int | None = None
    threshold_ms: int | None = None
    sample_pending: bool = False
    expires_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class CensoredRecord:
    """A request this node stopped waiting for but may still answer (B9)."""

    actor: str
    message_id: str
    requester: str
    received_at_ms: int
    threshold_ms: int
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class LedgerUpdate:
    """One inbox transaction's worth of durable link bookkeeping (§3.6)."""

    release: tuple[str, ...] = ()
    reasons: Mapping[str, str] = field(default_factory=dict)
    fail: Mapping[str, str] = field(default_factory=dict)
    drain: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    cancel_outbound: Mapping[str, str] = field(default_factory=dict)
    release_outbound: tuple[tuple[str, str], ...] = ()
    censor: tuple[CensoredRecord, ...] = ()
    sampled: tuple[tuple[str, str], ...] = ()
    open_outbound: tuple[LedgerRow, ...] = ()
    close_inbound: tuple[tuple[str, str, int], ...] = ()
    bind_tasks: tuple[tuple[str, str, str, int], ...] = ()
    unbind_tasks: tuple[tuple[str, str, str], ...] = ()
    purge_before_ms: int | None = None


@dataclass(frozen=True, slots=True)
class LedgerResult:
    """What actually happened, so memory only merges real successes (B15)."""

    released: tuple[str, ...] = ()
    expired: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    drained: tuple[tuple[str, str], ...] = ()
    cancelled: tuple[str, ...] = ()


class LinkInboxPort(Protocol):
    """Durable inbox facts the service consumes, all on the service thread."""

    def ledger_snapshot(self, now_ms: int) -> tuple[LedgerRow, ...]: ...

    def task_bindings(self) -> tuple[tuple[str, str, str, int], ...]: ...

    def held_outbox(self) -> tuple[HeldRow, ...]: ...

    def inbound_live(
        self, actor: str, peer: str | None = None
    ) -> tuple[InboundRow, ...]: ...

    def inbound_row(self, message_id: str) -> InboundRow | None: ...

    def apply(self, update: LedgerUpdate, *, now_ms: int) -> LedgerResult: ...


class LinkPresencePort(Protocol):
    """Canonical agent URIs currently online."""

    def online_agents(self) -> tuple[str, ...]: ...


class LinkPacPort(Protocol):
    """Read-only PAC durable state (B16)."""

    def task_states(
        self, task_ids: frozenset[str]
    ) -> Mapping[str, Literal["active", "ended", "unknown"]]: ...

    def worker_active(self, task_id: str, worker: str) -> bool | None: ...

    def is_workflow_worker(self, actor: str) -> bool:
        """Whether PAC owns ``actor`` as a temporary workflow worker.

        Read once per local actor from the desired-state in-memory view, so
        the service keeps such workers out of the proactive empty-file roster
        (E2E-021) without opening the PAC store or caching a verdict.
        """

        ...


class LinkStore:
    """The local ``link/`` copy plus pending writes (§1.1).

    All mutation entry points except :meth:`write` and :meth:`index` are
    service-thread only; the watch callback runs on an orgfs thread and only
    forwards to ``on_change``.
    """

    def __init__(
        self,
        fs: OrgFs,
        *,
        spaces: Callable[[], tuple[str, ...]],
        author_of: Callable[[str], str | None],
        is_local_agent: Callable[[str], bool],
        on_change: Callable[[], None],
        logger: Callable[..., None] | None = None,
        members_of: Callable[[str], Iterable[str]] | None = None,
        bookkeeping: bool = True,
    ) -> None:
        self._fs = fs
        self._bookkeeping = bookkeeping
        self._members_of = members_of
        self._members: frozenset[str] = frozenset()
        self._spaces = spaces
        self._author_of = author_of
        self._is_local_agent = is_local_agent
        self._on_change = on_change
        self._logger = logger
        self._index = LinkIndex()
        self._intents: dict[str, LinkFile] = {}
        self._live_spaces: set[str] = set()
        self._watches: dict[str, Any] = {}
        self._writable: set[str] = set()
        self._probed: set[str] = set()
        self._ambiguous_logged: set[str] = set()
        self._flush_skipped = False
        self._directories: dict[str, tuple[str, ...]] = {}
        self._canonical_directories: dict[str, str] = {}
        self._canonical_files: dict[tuple[str, str], str] = {}
        self._versions: dict[tuple[str, str], str] = {}
        self._files: dict[str, LinkFile] = {}
        self._revisions: dict[str, int] = {}
        self._conflicted: set[str] = set()
        self._observed: set[str] = set()
        self._written: set[str] = set()
        self._rejected: set[tuple[str, str, str]] = set()
        self._conflicts_logged: set[tuple[str, int, tuple[str, ...]]] = set()

    # -- readers (any thread) -------------------------------------------

    def index(self) -> LinkIndex:
        """Any thread; no I/O, no lock."""

        return self._index

    @property
    def bookkeeping(self) -> bool:
        return self._bookkeeping

    def intents(self) -> Mapping[str, LinkFile]:
        """Any thread; pending local writes."""

        return MappingProxyType(dict(self._intents))

    # -- service thread --------------------------------------------------

    def refresh(self) -> LinkIndex:
        """Service thread only: rescan the local copies and publish."""

        live = set(self._spaces())
        added = live - self._live_spaces
        removed = self._live_spaces - live
        for space in sorted(added):
            self._attach(space)
        for space in sorted(removed):
            self._detach(space)
        self._live_spaces = live
        if added or removed:
            self._log(
                "link.attach.summary",
                attached=sorted(s for s in added if self._watches.get(s) is not None),
                skipped=sorted(removed),
                failed=sorted(s for s in added if self._watches.get(s) is None),
                bookkeeping=self._bookkeeping,
            )
        for space in sorted(live):
            self._probe(space)
        self._scan(live)
        self._members = self._scan_members(live)
        self._publish()
        return self._index

    def write(self, file: LinkFile) -> None:
        """Service thread only: record a pending local write (B13)."""

        if not self._bookkeeping:
            self._intents.pop(file.actor, None)
            return
        if not self._is_local_agent(file.actor):
            raise ValueError(f"not a local agent of this node: {file.actor!r}")
        if self._author_of(file.actor) is None:
            raise ValueError(f"unknown owner for {file.actor!r}")
        self._intents[file.actor] = file

    def flush(self) -> tuple[str, ...]:
        """Service thread only: try to commit pending intents; return actors."""

        committed: list[str] = []
        if not self._bookkeeping:
            self._intents.clear()
            return ()
        if self._intents and not self._writable:
            if not self._flush_skipped:
                self._flush_skipped = True
                self._log(
                    "link.flush_skipped",
                    reason="no-writable-space",
                    intents=len(self._intents),
                )
            return ()
        self._flush_skipped = False
        for actor, file in list(self._intents.items()):
            spaces = sorted(self._writable)
            if not spaces:
                continue
            outcome = self._write_everywhere(actor, file, spaces)
            if outcome is True:
                self._commit(actor, file)
                committed.append(actor)
            elif outcome == "dropped":
                self._intents.pop(actor, None)
        return tuple(committed)

    def close(self) -> None:
        """Service thread only: drop watches; pending intents are abandoned."""

        for registration in self._watches.values():
            self._close_registration(registration)
        self._watches.clear()
        self._live_spaces.clear()

    # -- internals -------------------------------------------------------

    def _attach(self, space: str) -> None:
        try:
            self._watches[space] = self._fs.watch(
                space, "link/*.json", self._on_watch
            )
        except Exception:  # noqa: BLE001 - a watch is an optimisation only
            self._watches[space] = None

    def _probe(self, space: str) -> None:
        """Recheck write access and select one stable directory for writes."""

        if self._bookkeeping:
            try:
                self._fs.mkdir(space, "link")
            except OrgFsError as error:
                if error.code not in {"invalid-argument", "ambiguous-path"}:
                    self._set_writable(space, False, error.code)
                    return
            except Exception:  # noqa: BLE001 - an unavailable space is not writable
                self._set_writable(space, False, "unavailable")
                return
        try:
            roots = self._fs.listdir(space, "")
        except OrgFsError as error:
            self._set_writable(space, False, error.code)
            return
        except Exception:  # noqa: BLE001 - an unavailable space is not writable
            self._set_writable(space, False, "unavailable")
            return
        directories = tuple(
            sorted(
                entry.node_id
                for entry in roots
                if entry.kind == "dir" and entry.name == "link"
            )
        )
        if not directories:
            self._set_writable(space, False, "unknown-doc")
            return
        self._directories[space] = directories
        self._canonical_directories[space] = directories[0]
        if len(directories) > 1 and space not in self._ambiguous_logged:
            self._ambiguous_logged.add(space)
            self._log(
                "link.space_ambiguous",
                space=space,
                candidates=list(directories),
                canonical=directories[0],
            )
        self._set_writable(space, True)

    def _set_writable(
        self, space: str, writable: bool, code: str | None = None
    ) -> None:
        was_writable = space in self._writable
        first_probe = space not in self._probed
        self._probed.add(space)
        if writable:
            self._writable.add(space)
            return
        self._writable.discard(space)
        self._directories.pop(space, None)
        self._canonical_directories.pop(space, None)
        if was_writable or first_probe:
            self._log("link.space_unwritable", space=space, code=code)

    def _detach(self, space: str) -> None:
        self._close_registration(self._watches.pop(space, None))
        self._writable.discard(space)
        self._probed.discard(space)
        self._directories.pop(space, None)
        self._canonical_directories.pop(space, None)
        for key in [key for key in self._versions if key[0] == space]:
            del self._versions[key]
        for key in [key for key in self._canonical_files if key[0] == space]:
            del self._canonical_files[key]

    def _close_registration(self, registration: Any) -> None:
        if registration is None:
            return
        try:
            registration.close()
        except Exception:  # noqa: BLE001 - closing a dead watch is harmless
            pass

    def _on_watch(self, _event: object) -> None:
        """Runs on an orgfs thread: never reads, never takes a lock."""

        self._on_change()

    def _scan(self, live: set[str]) -> None:
        candidates: dict[str, list[LinkFile]] = {}
        self._canonical_files = {
            key: value
            for key, value in self._canonical_files.items()
            if key[0] not in live
        }
        self._versions = {
            key: value
            for key, value in self._versions.items()
            if key[0] not in live
        }
        for space in sorted(live):
            canonical = self._canonical_directories.get(space)
            for directory in self._directories.get(space, ()):
                try:
                    entries = self._fs.listdir(space, f"id:{directory}")
                except OrgFsError as error:
                    if error.code in _MEMBERSHIP_CODES:
                        self._set_writable(space, False, error.code)
                    continue
                except Exception:  # noqa: BLE001 - a broken space yields no copies
                    continue
                write_nodes = {
                    name: min(
                        entry.node_id
                        for entry in entries
                        if entry.kind == "doc" and entry.name == name
                    )
                    for name in {
                        entry.name for entry in entries if entry.kind == "doc"
                    }
                    if name.endswith(".json")
                }
                for entry in entries:
                    name = getattr(entry, "name", "")
                    if getattr(entry, "kind", "doc") != "doc" or not name.endswith(
                        ".json"
                    ):
                        continue
                    actor = name[:-5]
                    is_canonical = (
                        directory == canonical
                        and entry.node_id == write_nodes.get(name)
                    )
                    if is_canonical:
                        self._canonical_files[(space, actor)] = entry.node_id
                    document = self._read(
                        space,
                        actor,
                        f"id:{entry.node_id}",
                        track_version=is_canonical,
                    )
                    if document is not None:
                        candidates.setdefault(actor, []).append(document)
        files: dict[str, LinkFile] = {}
        conflicted: set[str] = set()
        for actor, items in candidates.items():
            high = max(self._revisions.get(actor, 0), self._own_revision(actor))
            highest = max(item.revision for item in items)
            if highest < high:
                # A lower snapshot than one already observed never rolls the
                # view back (B11/B12); our own committed write is kept so a
                # lagging mirror cannot erase it.
                if actor in self._written and actor in self._files:
                    files[actor] = self._files[actor]
                continue
            top = [item for item in items if item.revision == highest]
            self._revisions[actor] = max(high, highest)
            self._observed.add(actor)
            bodies = {_flatten(item) for item in top}
            if len(bodies) > 1:
                conflicted.add(actor)
                key = (actor, highest, tuple(sorted(live)))
                if key not in self._conflicts_logged:
                    self._conflicts_logged.add(key)
                    self._log(
                        "link.file_conflict",
                        actor=actor,
                        revision=highest,
                        detail="same revision, different content",
                    )
            else:
                files[actor] = top[0]
        self._files = files
        self._conflicted = conflicted

    def _own_revision(self, actor: str) -> int:
        committed = self._files.get(actor)
        return committed.revision if committed is not None else 0

    def _read(
        self,
        space: str,
        actor: str,
        path: str,
        *,
        track_version: bool,
    ) -> LinkFile | None:
        try:
            snapshot = self._fs.read_text_snapshot(space, path)
        except Exception:  # noqa: BLE001 - no readable copy means absent (B11)
            if track_version:
                self._versions.pop((space, actor), None)
            return None
        version = str(snapshot.version)
        if track_version:
            self._versions[(space, actor)] = version
        key = (space, path, version)

        def reject(detail: str) -> None:
            if key not in self._rejected:
                self._rejected.add(key)
                self._log(
                    "link.file_rejected",
                    actor=actor,
                    space=space,
                    node=path,
                    version=version,
                    detail=detail,
                )

        expected = self._author_of(actor)
        if expected is None:
            reject("unknown-author")
            return None
        try:
            authors = self._fs.document_authors(space, path)
        except Exception:  # noqa: BLE001 - an unreadable author list is a reject
            reject("author-unreadable")
            return None
        if not authors or any(author != expected for author in authors):
            reject("author-mismatch")
            return None
        try:
            document = _parse(snapshot.content)
        except (ValueError, TypeError):
            reject("malformed-document")
            return None
        if document.actor != actor:
            reject("actor-mismatch")
            return None
        return document

    def _write_everywhere(
        self, actor: str, file: LinkFile, spaces: list[str]
    ) -> bool | str:
        if not self._bookkeeping:
            return False
        ok = True
        filename = link_file_path(actor).removeprefix("link/")
        for space in spaces:
            directory = self._canonical_directories.get(space)
            if directory is None:
                ok = False
                continue
            existing = self._canonical_files.get((space, actor))
            path = f"id:{existing}" if existing else f"id:{directory}/{filename}"
            try:
                node = self._fs.write_text(
                    space,
                    path,
                    _flatten(file),
                    expect_version=self._versions.get((space, actor)),
                )
            except OrgFsError as error:
                if error.code == "stale-write":
                    outcome = self._resolve_stale(space, actor, file, path)
                    if outcome == "dropped":
                        return "dropped"
                    ok = ok and outcome is True
                elif error.code in _MEMBERSHIP_CODES:
                    self._set_writable(space, False, error.code)
                    ok = False
                else:
                    ok = False
            except Exception:  # noqa: BLE001 - retry this mirror next pass
                ok = False
            else:
                self._canonical_files[(space, actor)] = node.node_id
                self._versions[(space, actor)] = str(node.version)
        return True if ok else False

    def _resolve_stale(
        self, space: str, actor: str, file: LinkFile, path: str
    ) -> bool | str | None:
        """Reread after a stale write, then rewrite or drop the intent (B13)."""

        try:
            snapshot = self._fs.read_text_snapshot(space, path)
        except Exception:  # noqa: BLE001 - retry rather than clobber blindly
            return None
        self._versions[(space, actor)] = str(snapshot.version)
        try:
            existing = _parse(snapshot.content)
        except (ValueError, TypeError):
            existing = None
        if existing is not None:
            self._observed.add(actor)
            self._revisions[actor] = max(
                self._revisions.get(actor, 0), existing.revision
            )
        if not self._is_local_agent(actor):
            return "dropped"
        if file.revision <= self._revisions.get(actor, 0):
            self._log(
                "link.intent_dropped",
                actor=actor,
                space=space,
                intentRevision=file.revision,
                observedRevision=self._revisions.get(actor),
            )
            return "dropped"
        try:
            node = self._fs.write_text(
                space, path, _flatten(file), expect_version=str(snapshot.version)
            )
        except Exception:  # noqa: BLE001 - retry this mirror next pass
            return None
        self._versions[(space, actor)] = str(node.version)
        self._log(
            "link.file_conflict",
            actor=actor,
            space=space,
            intentRevision=file.revision,
            observedRevision=self._revisions.get(actor),
            detail="stale-write resolved",
        )
        return True

    def _commit(self, actor: str, file: LinkFile) -> None:
        # CAS (WP2-A rework P-3): while this intent was being written, another
        # thread may have recorded a newer one.  Only the intent we actually
        # committed may be cleared; a newer one stays pending for the next
        # flush, otherwise a concurrent retraction or mint is lost forever.
        if self._intents.get(actor) is file:
            self._intents.pop(actor, None)
        self._written.add(actor)
        self._observed.add(actor)
        self._revisions[actor] = max(self._revisions.get(actor, 0), file.revision)
        self._conflicted.discard(actor)
        self._files[actor] = file
        self._publish()

    def _publish(self) -> None:
        absent = {
            actor
            for actor in self._observed
            if actor not in self._files and actor not in self._conflicted
        }
        self._index = LinkIndex(
            spaces=frozenset(self._live_spaces),
            writable=frozenset(self._writable),
            files=dict(self._files),
            revisions=dict(self._revisions),
            absent=frozenset(absent),
            conflicted=frozenset(self._conflicted),
            members=self._members,
        )

    def _scan_members(self, live: set[str]) -> frozenset[str]:
        """Members of every live space; an unreadable space adds nobody."""

        if self._members_of is None:
            return frozenset()
        members: set[str] = set()
        for space in sorted(live):
            try:
                members.update(
                    user for user in self._members_of(space) if isinstance(user, str)
                )
            except Exception:  # noqa: BLE001 - fail closed: no member proof
                continue
        return frozenset(members)

    def _log(self, event: str, **fields: object) -> None:
        if self._logger is None:
            return
        try:
            self._logger("info", "daemon", event, **fields)
        except Exception:  # noqa: BLE001 - logging never breaks the store
            pass


def _flatten(document: LinkFile) -> str:
    """The compact, key-sorted encoding written to orgfs (§1.1)."""

    return json.dumps(to_document(document), sort_keys=True, separators=(",", ":"))


def _parse(content: str) -> LinkFile:
    return from_document(json.loads(content))
