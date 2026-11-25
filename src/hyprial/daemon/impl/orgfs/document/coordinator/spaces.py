# Decision record (phase D, package D1, 2026-11-25):
# - Design §3.3 / §9 R7: ``create_space`` writes the meta ``space`` root, the
#   owner's member row and the tree root row in one batch through the commit
#   helper; the doc id of the tree is always ``tree``.  A failed creation drops
#   the space and discards its store.
# - ``load_space`` opens a space from its store: meta and tree only, every
#   document row starts unverified (§2.3, §9 R4); a tree without a root row
#   gets an in-memory placeholder and local writes refuse until the row
#   arrives (§9 R7, ``content-pending``).
# - Design §3.2 / §9 R8, P0 report §8: members are written with
#   ``put_member`` / ``mark_member_removed``; removal keeps the row with
#   ``removedAt``; ``members()`` is sorted by user.  Timestamps are microsecond
#   ISO-8601 UTC so a re-add can be ordered after the previous ``addedAt``.
# - Design §3.4: local writes need a current ``rw`` member
#   (``not-a-member``) and, per written row, the protected-path rule of
#   ``membership.check_local_write``; member and meta changes are owner only.
# - Design §1.1 / §4.6: ``status`` returns ``StateSpaceStatus`` (the
#   ``OrgFs`` Protocol and its ``SpaceStatus`` change in package D4b).
#   ``synced`` is ``None`` without an online holder observation, ``True`` when
#   one observation covers the local meta and tree vectors and nothing is
#   pending or unverified, else ``False``.
#   F4 ruling (design question 2, option 1): ``True`` also needs the local
#   meta and tree vectors to cover every current observation; while a holder
#   is observed ahead of the local catalog (e.g. a joined tree that has not
#   arrived) ``synced`` is ``False``.  Observations come from the
#   attached sync (``attach_sync``); D1 tests attach a fake.
# - D2, design §9 R5: a join first opens a temporary space (``begin_join``):
#   a v3 directory, an empty registry and a placeholder root, hidden from
#   ``spaces()`` and from ``join``'s early return.  Accepted meta adopts the
#   ``space`` root at once so tree admission sees the real owner and name;
#   ``complete_join`` requires an owner and this node's own member row, then
#   unhides the space; ``abandon_join`` stops the space's command loop,
#   forgets the space and lets the store discard remove the directory; a
#   later join of the same space starts afresh.
# - D2, design §2.7: a space the runtime's startup scan refused is recorded
#   with ``refuse_space``; every lookup of it raises that error
#   (``ORGFS_LAYOUT_UNSUPPORTED`` with its ``reason``) instead of
#   ``unknown-space``.
"""Spaces, members and status of the state host."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import time
import uuid
from typing import Any, Callable, Mapping

from pycrdt import Map

from hyprial.kernel import ORGFS_CONTENT_PENDING

from hyprial.daemon.impl.orgfs.api import (
    MemberInfo,
    MemberMode,
    NodeRef,
    OrgFsError,
    SpaceInfo,
    require_space_owner,
)
from hyprial.daemon.impl.orgfs.document.coordinator.host import (
    AdmissionViewOf,
    SpaceSyncHost,
    state_command,
)
from hyprial.daemon.impl.orgfs.document.coordinator.registry import DocRegistry, SealedContents
from hyprial.daemon.impl.orgfs.document.model import _Node, _Space, _facade_locked
from hyprial.daemon.impl.orgfs.membership import (
    LocalWrite,
    ProtectionPolicy,
    check_local_write,
    document_authors,
    mark_member_removed,
    put_member,
    read_member_rows,
    read_space,
    vector_covers,
    write_space,
)
from hyprial.daemon.impl.orgfs.state import MemoryBacking, MemoryStateStore, StateStore


def timestamp() -> str:
    """Microsecond ISO-8601 UTC timestamp ending in ``Z``."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class StateSpaceStatus:
    """Design §1.1 space status of the state host."""

    space_id: str
    synced: bool | None
    holders_online: tuple[str, ...]
    content_pending: int
    rejected_updates: int


class MemoryStores:
    """Default store factory: one ``MemoryStateStore`` backing per space."""

    def __init__(self) -> None:
        self.backings: dict[str, MemoryBacking] = {}

    def __call__(self, space_id: str, create: bool) -> StateStore:
        backing = self.backings.get(space_id)
        if backing is None:
            if not create:
                raise OrgFsError("unknown-space", {"spaceId": space_id})
            backing = self.backings[space_id] = MemoryBacking(space_id)
        return MemoryStateStore(space_id, backing=backing)

    def discard(self, space_id: str) -> None:
        self.backings.pop(space_id, None)


class StateSpaces:
    """Space creation and loading, members, authors and status of ``LocalOrgFs``."""

    _store_factory: Callable[[str, bool], StateStore]
    _store_discard: Callable[[str], None] | None

    def _new_space(self, info: SpaceInfo, registry: DocRegistry) -> _Space:
        space = _Space(
            info,
            "tree",
            registry.tree,
            registry.meta,
            {},
            SealedContents(),
            {},
            set(),
        )
        registry.on_meta = lambda doc: setattr(space, "meta", doc)
        return space

    @_facade_locked
    def create_space(self, name: str, *, space_id: str | None = None) -> SpaceInfo:
        if not isinstance(name, str) or not name.strip():
            raise OrgFsError("invalid-argument", {"message": "space name is required"})
        space_id = str(uuid.uuid4()) if space_id is None else str(uuid.UUID(space_id))
        if self._existing_space(space_id) is not None:
            raise OrgFsError("already-exists", {"spaceId": space_id})
        created_at = timestamp()
        info = SpaceInfo(space_id, name, self.author, created_at)
        registry = DocRegistry(
            space_id, self._store_factory(space_id, True), capacity=self._content_capacity
        )
        space = self._new_space(info, registry)
        root = _Node("root", None, "", "dir", modified_by=self.author)
        space.nodes["root"] = root
        space.members = {self.author: MemberInfo(space_id, self.author, "rw", self.author, created_at)}
        with self._lock:
            self._registries[space_id] = registry
            self._spaces[space_id] = space

        def initialize_meta() -> None:
            write_space(
                space.meta, space_id=space_id, name=name, owner=self.author, created_at=created_at
            )
            put_member(space.meta, self.author, mode="rw", added_by=self.author, added_at=created_at)

        def initialize_tree() -> None:
            self._record_row(space, root)

        try:
            self._commit_many(space, (("meta", initialize_meta), ("tree", initialize_tree)))
        except Exception:
            with self._lock:
                self._spaces.pop(space_id, None)
                self._registries.pop(space_id, None)
            try:
                registry.store.close()
            finally:
                if self._store_discard is not None:
                    self._store_discard(space_id)
            raise
        self._refresh_versions(space, ["root"])
        return info

    def spaces(self) -> tuple[SpaceInfo, ...]:
        with self._lock:
            return tuple(
                space.info
                for space_id, space in self._spaces.items()
                if space_id not in self._joining
            )

    def _space(self, space_id: str) -> _Space:
        with self._lock:
            space = self._spaces.get(space_id)
            refused = self._refused_spaces.get(space_id)
        if space is not None:
            return space
        if refused is not None:
            raise OrgFsError(refused[0], dict(refused[1]))
        raise OrgFsError("unknown-space", {"spaceId": space_id})

    def _existing_space(self, space_id: str) -> _Space | None:
        with self._lock:
            return self._spaces.get(space_id)

    def refuse_space(self, space_id: str, code: str, details: Mapping[str, object]) -> None:
        """Answer every later lookup of ``space_id`` with ``code`` (design §2.7)."""

        with self._lock:
            self._refused_spaces[space_id] = (code, dict(details))

    @_facade_locked
    def join(self, space_id: str, *, timeout: float | None = None) -> SpaceInfo:
        with self._lock:
            joining = space_id in self._joining
        if joining:
            raise OrgFsError("unavailable", {"spaceId": space_id, "message": "join in progress"})
        existing = self._existing_space(space_id)
        if existing is not None:
            return existing.info
        if self.mesh is None:
            raise OrgFsError("no-holder-online")
        info = self.mesh.join(space_id) if timeout is None else self.mesh.join(space_id, timeout=timeout)
        if not isinstance(info, SpaceInfo):
            raise OrgFsError(
                "invalid-argument", {"message": "mesh returned invalid space info"}
            )
        return info

    def begin_join(self, space_id: str) -> None:
        """Open the temporary space a join fills (design §9 R5)."""

        with self._lock:
            if space_id in self._spaces:
                raise OrgFsError("already-exists", {"spaceId": space_id})
        store = self._store_factory(space_id, True)
        registry = DocRegistry(space_id, store, capacity=self._content_capacity)
        try:
            registry.load_directory()
        except Exception:
            store.close()
            raise
        space = self._new_space(SpaceInfo(space_id, "", "", ""), registry)
        space.nodes["root"] = _Node("root", None, "", "dir")
        with self._lock:
            self._detached_spaces.discard(space_id)
            self._joining.add(space_id)
            self._registries[space_id] = registry
            self._spaces[space_id] = space

    def _adopt_space_root(self, space: _Space) -> None:
        """Take name and owner of a joining space from its meta."""

        with self._lock:
            joining = space.info.space_id in self._joining
        if not joining:
            return
        raw = read_space(space.meta)
        owner = raw.get("owner")
        if isinstance(owner, str) and owner:
            space.info = SpaceInfo(
                space.info.space_id,
                str(raw.get("name", space.info.space_id)),
                owner,
                str(raw.get("createdAt", "")),
            )

    def complete_join(self, space_id: str) -> SpaceInfo:
        """Unhide a joined space once its meta names an owner and this node's member."""

        with self._space_lock(space_id):
            space = self._space(space_id)
            registry = self._registry(space_id)
            self._adopt_space_root(space)
            if not space.info.owner:
                raise OrgFsError(
                    "no-holder-online", {"spaceId": space_id, "reason": "meta-incomplete"}
                )
            if self.author not in space.members:
                raise OrgFsError(
                    "not-a-member", {"spaceId": space_id, "reason": "not-invited"}
                )
            for node in space.nodes.values():
                if (
                    node.kind == "doc"
                    and node.doc_id
                    and node.doc_id not in registry.pending
                    and registry.loaded(node.doc_id) is None
                ):
                    registry.mark(node.doc_id, None)
            with self._lock:
                self._joining.discard(space_id)
            return space.info

    def abandon_join(self, space_id: str, timeout: float = 5.0) -> bool:
        """Drop a temporary space and its directory (design §9 R5).

        It runs inside the caller's own ``join``, whose effect reservation is
        still open, so it stops the space's command loop itself instead of
        ``detach_space`` (which waits for every effect of the facade).
        """

        with self._lock:
            if space_id not in self._joining:
                return False
            self._syncs.pop(space_id, None)
            owner = self._space_state_owners.get(space_id)
        stopped = True
        if owner is not None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                snapshot = owner.runtime.snapshot()
                if snapshot.queued == 0 and snapshot.in_flight == 0 and not owner.deferred:
                    break
                time.sleep(0.005)
            remaining = max(0.0, deadline - time.monotonic())
            stopped = owner.effects.close(remaining) and owner.runtime.stop(
                timeout=max(0.0, deadline - time.monotonic())
            )
        with self._space_lock(space_id):
            with self._lock:
                self._joining.discard(space_id)
                self._spaces.pop(space_id, None)
                self._space_state_owners.pop(space_id, None)
                self._events.pop(space_id, None)
                self._publish_queues.pop(space_id, None)
                registry = self._registries.pop(space_id, None)
        if registry is not None:
            try:
                registry.store.close()
            except Exception:
                pass
        if self._store_discard is not None:
            self._store_discard(space_id)
        return stopped

    @_facade_locked
    def load_space(self, space_id: str) -> SpaceInfo:
        """Open a space from its store: meta and tree now, content on first use."""

        existing = self._existing_space(space_id)
        if existing is not None:
            return existing.info
        store = self._store_factory(space_id, False)
        registry = DocRegistry(space_id, store, capacity=self._content_capacity)
        try:
            registry.load_directory()
        except Exception:
            store.close()
            raise
        raw = read_space(registry.meta)
        owner = raw.get("owner")
        if not isinstance(owner, str) or not owner:
            store.close()
            raise OrgFsError("unknown-space", {"spaceId": space_id})
        info = SpaceInfo(
            space_id,
            str(raw.get("name", space_id)),
            owner,
            str(raw.get("createdAt", "")),
        )
        space = self._new_space(info, registry)
        with self._lock:
            self._registries[space_id] = registry
            self._spaces[space_id] = space
        self._rematerialize(space)
        for node in space.nodes.values():
            if node.kind == "doc" and node.doc_id:
                registry.mark(node.doc_id, None)
        return info

    def _refresh_members(self, space: _Space) -> None:
        rows = read_member_rows(space.meta)
        space_id = space.info.space_id
        space.members = {
            user: MemberInfo(space_id, user, row.mode, row.added_by, row.added_at)
            for user, row in sorted(rows.items())
            if row.is_member
        }
        space.removed_members = {user for user, row in rows.items() if not row.is_member}

    @_facade_locked
    def invite(self, space_id: str, user: str, mode: MemberMode = "rw") -> MemberInfo:
        space = self._space(space_id)
        require_space_owner(self.author, space.info)
        if mode not in ("ro", "rw") or not isinstance(user, str) or not user:
            raise OrgFsError("invalid-argument")
        written: list[Any] = []

        def operation() -> None:
            written.append(
                put_member(space.meta, user, mode=mode, added_by=self.author, added_at=timestamp())
            )

        self._commit(space, "meta", operation)
        row = written[0]
        return MemberInfo(space_id, user, row.mode, row.added_by, row.added_at)

    @state_command
    def update_space_meta(
        self, space_id: str, values: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Merge owner-only application metadata into the space meta, as a space command."""

        space = self._space(space_id)
        require_space_owner(self.author, space.info)
        if not isinstance(values, Mapping) or not values:
            raise OrgFsError(
                "invalid-argument", {"message": "space metadata values are required"}
            )
        if any(not isinstance(key, str) or not key for key in values):
            raise OrgFsError(
                "invalid-argument", {"message": "space metadata keys must be strings"}
            )
        if {"name", "owner", "createdAt", "spaceId"}.intersection(values):
            raise OrgFsError(
                "invalid-argument", {"message": "core space metadata is immutable"}
            )

        def operation() -> None:
            meta = space.meta.get("space", type=Map)
            for key, value in values.items():
                meta[key] = value

        self._commit(space, "meta", operation)
        return self.space_meta(space_id)

    @_facade_locked
    def remove_member(self, space_id: str, user: str) -> None:
        space = self._space(space_id)
        require_space_owner(self.author, space.info)
        if user == space.info.owner or user not in space.members:
            raise OrgFsError("invalid-argument")

        def operation() -> None:
            mark_member_removed(space.meta, user, removed_at=timestamp())

        self._commit(space, "meta", operation)

    @_facade_locked
    def members(self, space_id: str) -> tuple[MemberInfo, ...]:
        return tuple(sorted(self._space(space_id).members.values(), key=lambda member: member.user))

    @_facade_locked
    def document_authors(self, space_id: str, node: NodeRef) -> tuple[str, ...]:
        """The document's author set ``__orgfs_authors__`` (design §3.3)."""

        space = self._space(space_id)
        item = self._one(space, node)
        if item.kind != "doc" or item.doc_id is None:
            raise OrgFsError("invalid-argument", {"message": "not a text document"})
        return document_authors(self._content_doc(space, item.doc_id).doc)

    @_facade_locked
    def status(self, space_id: str) -> StateSpaceStatus:
        space = self._space(space_id)
        registry = self._registry(space_id)
        with self._lock:
            sync = self._syncs.get(space_id)
        observations = tuple(sync.observations()) if sync is not None and hasattr(sync, "observations") else ()
        pending = len(registry.pending) + len(registry.unverified)
        synced: bool | None = None
        if observations:
            meta = space.meta.get_state()
            tree = space.tree.doc.get_state()
            covered = any(
                vector_covers(observation.meta_vector, meta)
                and vector_covers(observation.tree_vector, tree)
                for observation in observations
            )
            behind = any(
                not (
                    vector_covers(meta, observation.meta_vector)
                    and vector_covers(tree, observation.tree_vector)
                )
                for observation in observations
            )
            synced = covered and not behind and pending == 0
        return StateSpaceStatus(
            space_id,
            synced,
            tuple(sorted({observation.node for observation in observations})),
            pending,
            registry.rejected_updates,
        )

    def _ensure_writable(self, space: _Space) -> None:
        if space.tree.row("root") is None:
            raise OrgFsError(
                ORGFS_CONTENT_PENDING,
                {"spaceId": space.info.space_id, "node": "id:root", "reason": "root-row-missing"},
            )
        member = space.members.get(self.author)
        if member is None or member.mode != "rw":
            raise OrgFsError("not-a-member", {"spaceId": space.info.space_id})

    def _write_view(self, space: _Space) -> tuple[AdmissionViewOf, ProtectionPolicy]:
        view = AdmissionViewOf(self, space)
        return view, ProtectionPolicy.from_view(view)

    def _check_row_write(self, space: _Space, node: _Node, extra_paths: tuple[str, ...] = ()) -> None:
        view, policy = self._write_view(space)
        paths = tuple(path for path in (self._path(space, node.node_id), *extra_paths) if path)
        check_local_write(
            self.author,
            LocalWrite("tree", paths=paths, node_ids=(node.node_id,), doc_id=node.doc_id),
            view,
            policy,
        )

    def attach_sync(self, space_id: str, sync: Any) -> None:
        """Attach the space's sync: publications and observations go to it."""

        self._space(space_id)
        with self._lock:
            self._syncs[space_id] = sync

    def detach_sync(self, space_id: str) -> None:
        with self._lock:
            self._syncs.pop(space_id, None)

    def sync_host(
        self, space_id: str, node_gate: Callable[[str], bool] | None = None
    ) -> SpaceSyncHost:
        """The ``SyncHost`` of ``space_id``; ``node_gate`` defaults to allow-all."""

        self._space(space_id)
        return SpaceSyncHost(self, space_id, node_gate or (lambda _node: True))


__all__ = ["MemoryStores", "StateSpaceStatus", "StateSpaces", "timestamp"]
