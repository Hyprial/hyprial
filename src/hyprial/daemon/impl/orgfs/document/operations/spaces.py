from __future__ import annotations

import base64









import json




import uuid

from collections.abc import Callable, Mapping



from pycrdt import Doc, Map






from hyprial.daemon.impl.orgfs.api  import (
    MemberInfo,
    MemberMode,
    OrgFsError,
    SpaceInfo,
    require_space_owner)




from hyprial.daemon.impl.orgfs.storage.space_authority  import (
    _ReadStore)





from hyprial.daemon.impl.orgfs.document.model import TreeDocument, _ContentDocument, _MetaWatch, _Node, _Space, _decode_content_frontier, _facade_locked, _now, _version

class FacadeSpaces:
    """Responsibility methods on the sole LocalOrgFs state host.

    This class never constructs, copies, or persists an independent host.
    """

    @_facade_locked
    def create_space(self, name: str) -> SpaceInfo:
        if not isinstance(name, str) or not name.strip():
            raise OrgFsError("invalid-argument", {"message": "space name is required"})
        space_id = str(uuid.uuid4())
        tree_doc_id = f"tree-{uuid.uuid4()}"
        info = SpaceInfo(space_id, name, self.author, _now())
        root = _Node("root", None, "", "dir", version="", modified_by=self.author)
        space = _Space(
            info,
            tree_doc_id,
            TreeDocument(),
            Doc(),
            {"root": root},
            {},
            {},
            set(),
            {},
            {},
        )
        space.tree.record(root)
        member = MemberInfo(space_id, self.author, "rw", self.author, info.created_at)
        space.members[self.author] = member
        self._spaces[space_id] = space

        def initialize() -> None:
            meta = space.meta.get("space", type=Map)
            meta.update(
                {
                    "name": name,
                    "owner": self.author,
                    "createdAt": info.created_at,
                    "spaceId": space_id,
                }
            )
            members = space.meta.get("members", type=Map)
            members[self.author] = Map(
                {
                    "mode": "rw",
                    "addedBy": self.author,
                    "addedAt": info.created_at,
                    "removedAt": None,
                }
            )
            docs = space.meta.get("docs", type=Map)
            docs["tree"] = Map(
                {
                    "active": tree_doc_id,
                    "activatedAt": info.created_at,
                    "planId": None,
                }
            )

        try:
            self._commit(space, "meta", initialize)
        except Exception:
            self._spaces.pop(space_id, None)
            raise
        return info


    @_facade_locked
    def spaces(self) -> tuple[SpaceInfo, ...]:
        return tuple(space.info for space in self._spaces.values())


    @_facade_locked
    def space_meta(self, space_id: str) -> Mapping[str, object]:
        """Return the replicated owner-written ``space`` metadata map."""

        space = self._space(space_id)
        meta = space.meta.get("space", type=Map)
        return dict(meta.to_py() or {})


    @_facade_locked
    def watch_meta(self, space_id: str, callback: Callable[[], None]) -> _MetaWatch:
        watcher = _MetaWatch(callback)
        self._space(space_id).meta_watches.append(watcher)
        with self._lock:
            self._meta_watchers[watcher.watch_id] = watcher
        return watcher


    @_facade_locked
    def update_space_meta(
        self, space_id: str, values: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Merge owner-only application metadata into the space meta doc."""

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
        immutable = {"name", "owner", "createdAt", "spaceId"}
        if immutable.intersection(values):
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
    def invite(self, space_id: str, user: str, mode: MemberMode = "rw") -> MemberInfo:
        space = self._space(space_id)
        require_space_owner(self.author, space.info)
        if mode not in ("ro", "rw") or not isinstance(user, str) or not user:
            raise OrgFsError("invalid-argument")
        # A re-invite of a current member changes its mode only: added_at
        # orders who dials whom between org devices (org.network.dialing), so
        # it must not move.  The owner's own row keeps the space's birth.
        current = space.members.get(user)
        if user == space.info.owner:
            member = MemberInfo(space_id, user, mode, self.author, space.info.created_at)
        elif current is not None and user not in space.removed_members:
            member = MemberInfo(space_id, user, mode, current.added_by, current.added_at)
        else:
            member = MemberInfo(space_id, user, mode, self.author, _now())

        def operation() -> None:
            space.members[user] = member
            space.removed_members.discard(user)
            members = space.meta.get("members", type=Map)
            members[user] = Map(
                {
                    "mode": mode,
                    "addedBy": member.added_by,
                    "addedAt": member.added_at,
                    "removedAt": None,
                }
            )

        self._commit(space, "meta", operation)
        return member


    @_facade_locked
    def remove_member(self, space_id: str, user: str) -> None:
        space = self._space(space_id)
        require_space_owner(self.author, space.info)
        if user == space.info.owner or user not in space.members:
            raise OrgFsError("invalid-argument")

        def operation() -> None:
            space.removed_members.add(user)
            member = space.members.pop(user)
            members = space.meta.get("members", type=Map)
            members[user] = Map(
                {
                    "mode": member.mode,
                    "addedBy": member.added_by,
                    "addedAt": member.added_at,
                    "removedAt": _now(),
                }
            )

        self._commit(space, "meta", operation)


    @_facade_locked
    def members(self, space_id: str) -> tuple[MemberInfo, ...]:
        return tuple(
            space_member for space_member in self._space(space_id).members.values()
        )


    @_facade_locked
    def join(self, space_id: str) -> SpaceInfo:
        existing = self._existing_space(space_id)
        if existing is not None:
            return existing.info
        if self.mesh is None:
            raise OrgFsError("no-holder-online")
        info = self.mesh.join(space_id)
        if not isinstance(info, SpaceInfo):
            raise OrgFsError(
                "invalid-argument", {"message": "mesh returned invalid space info"}
            )
        return info


    @_facade_locked
    def load_space(self, space_id: str) -> SpaceInfo:
        """Hydrate the local facade after an empty-VV mesh clone."""

        existing = self._existing_space(space_id)
        if existing is not None:
            return existing.info
        store = self._store(space_id)
        if store is None:
            raise OrgFsError("unknown-space", {"spaceId": space_id})
        meta = Doc()
        meta.apply_update(store.snapshot("meta", shallow_since=None).snapshot_bytes)
        space_root = meta.get("space", type=Map)
        raw_space = space_root.to_py() if space_root is not None else None
        if not isinstance(raw_space, dict):
            raise OrgFsError("unknown-space", {"spaceId": space_id})
        info = SpaceInfo(
            space_id,
            str(raw_space.get("name", space_id)),
            str(raw_space.get("owner", "")),
            str(raw_space.get("createdAt", "")),
        )
        raw_docs = meta.get("docs", type=Map)
        docs = raw_docs.to_py() if raw_docs is not None else None
        tree_mapping = docs.get("tree") if isinstance(docs, dict) else None
        tree_doc_id = (
            tree_mapping.get("active") if isinstance(tree_mapping, dict) else None
        )
        if not isinstance(tree_doc_id, str) or not tree_doc_id.startswith("tree-"):
            raise OrgFsError("unknown-doc", {"message": "active tree mapping missing"})
        tree = TreeDocument(
            update=store.snapshot(tree_doc_id, shallow_since=None).snapshot_bytes
        )
        nodes: dict[str, _Node] = {}
        contents: dict[str, _ContentDocument] = {}
        for node_id, raw in tree.materialize().items():
            node = _Node(
                node_id=node_id,
                parent=raw.get("parent"),
                name=str(raw.get("name", "")),
                kind=raw.get("kind", "doc"),
                doc_id=raw.get("doc_id"),
                blob_hash=raw.get("blob_hash"),
                size=raw.get("size") if type(raw.get("size")) is int else None,
                required_content_frontier=_decode_content_frontier(
                    raw.get("contentFrontier")
                ),
                ref_size=(
                    raw.get("refSize") if type(raw.get("refSize")) is int else None
                ),
                ref_sha256=(
                    raw.get("refSha256")
                    if isinstance(raw.get("refSha256"), str)
                    else None
                ),
                deleted=bool(raw.get("deleted", False)),
                modified_by=info.owner,
            )
            nodes[node_id] = node
            if node.doc_id:
                document = _ContentDocument(
                    update=store.snapshot(
                        node.doc_id, shallow_since=None
                    ).snapshot_bytes
                )
                contents[node.doc_id] = document
        if "root" not in nodes:
            nodes["root"] = _Node("root", None, "", "dir", modified_by=info.owner)
        members: dict[str, MemberInfo] = {}
        raw_members = meta.get("members", type=Map)
        for user, raw in (
            (raw_members.to_py() or {}).items() if raw_members is not None else ()
        ):
            if not isinstance(raw, dict) or raw.get("removedAt"):
                continue
            mode = raw.get("mode")
            if mode not in {"ro", "rw"}:
                continue
            members[str(user)] = MemberInfo(
                space_id,
                str(user),
                mode,
                str(raw.get("addedBy", info.owner)),
                str(raw.get("addedAt", info.created_at)),
            )
        loaded = _Space(
            info,
            tree_doc_id,
            tree,
            meta,
            nodes,
            contents,
            members,
            set(),
            {},
            {},
        )
        # A freshly cloned facade still needs a valid local base version for
        # the public write_text(base_version=...) contract.  The durable store
        # has the CRDT state but does not persist the facade's in-memory
        # version labels, so establish an opaque baseline for the hydrated
        # state and retain its snapshot just like a local commit does.
        baseline = _version(0, 0)
        for node in loaded.nodes.values():
            node.version = baseline
        loaded.snapshots[baseline] = self._snapshot(loaded)
        with self._lock:
            self._spaces.setdefault(space_id, loaded)
            loaded = self._spaces[space_id]
        return info


    @_facade_locked
    def apply_envelope(self, space_id: str, envelope: bytes) -> None:
        """Refresh an open facade after the durable store admits an envelope."""

        space = self._existing_space(space_id)
        if space is None:
            return
        try:
            value = json.loads(envelope)
            origin = value["origin"]
            doc_id = str(value["docId"])
            if "update" in value:
                update = base64.b64decode(str(value["update"]), validate=True)
            else:
                if self.blobs is None:
                    raise OrgFsError(
                        "blob-unavailable", {"digest": value.get("updateBlob")}
                    )
                update = self.blobs.get(space_id, str(value["updateBlob"]))
        except OrgFsError:
            raise
        except Exception as exc:
            raise OrgFsError(
                "invalid-argument", {"message": "invalid admitted envelope"}
            ) from exc

        origin_author = str(origin.get("author", ""))
        origin_actor = (
            origin.get("actor") if isinstance(origin.get("actor"), str) else None
        )

        def refresh() -> None:
            if doc_id == "meta":
                old_retirements_root = space.meta.get("retirements", type=Map)
                old_retirements = set(
                    (old_retirements_root.to_py() or {})
                    if old_retirements_root is not None
                    else ()
                )
                space.meta.apply_update(update)
                raw_members = space.meta.get("members", type=Map)
                active: dict[str, MemberInfo] = {}
                removed: set[str] = set()
                for user, raw in (
                    (raw_members.to_py() or {}).items()
                    if raw_members is not None
                    else ()
                ):
                    if not isinstance(raw, dict):
                        continue
                    if raw.get("removedAt"):
                        removed.add(str(user))
                        continue
                    mode = raw.get("mode")
                    if mode not in {"ro", "rw"}:
                        continue
                    active[str(user)] = MemberInfo(
                        space_id,
                        str(user),
                        mode,
                        str(raw.get("addedBy", space.info.owner)),
                        str(raw.get("addedAt", space.info.created_at)),
                    )
                space.members = active
                space.removed_members = removed
                retirements_root = space.meta.get("retirements", type=Map)
                retirements = (
                    retirements_root.to_py() or {}
                    if retirements_root is not None
                    else {}
                )
                for retired_doc_id in set(retirements) - old_retirements:
                    record = retirements.get(retired_doc_id)
                    if isinstance(record, dict):
                        self._export_behind_writer_work(
                            space, retired_doc_id, str(record.get("planId", ""))
                        )
                return
            if doc_id == space.tree_doc_id:
                previous = {
                    node_id: (
                        node.parent,
                        node.name,
                        node.kind,
                        node.blob_hash,
                        node.deleted,
                    )
                    for node_id, node in space.nodes.items()
                }
                old_paths = {
                    node_id: self._path(space, node_id) for node_id in space.nodes
                }
                space.tree.apply_update(update)
                materialized = space.tree.materialize()
                for node_id, raw in materialized.items():
                    if node_id not in space.nodes:
                        space.nodes[node_id] = _Node(
                            node_id=node_id,
                            parent=raw.get("parent"),
                            name=str(raw.get("name", "")),
                            kind=raw.get("kind", "doc"),
                            doc_id=raw.get("doc_id"),
                            blob_hash=raw.get("blob_hash"),
                            size=(
                                raw.get("size")
                                if type(raw.get("size")) is int
                                else None
                            ),
                            required_content_frontier=_decode_content_frontier(
                                raw.get("contentFrontier")
                            ),
                            ref_size=(
                                raw.get("refSize")
                                if type(raw.get("refSize")) is int
                                else None
                            ),
                            ref_sha256=(
                                raw.get("refSha256")
                                if isinstance(raw.get("refSha256"), str)
                                else None
                            ),
                            deleted=bool(raw.get("deleted", False)),
                            modified_by=origin_author,
                            modified_via=origin_actor,
                        )
                    else:
                        node = space.nodes[node_id]
                        node.parent = raw.get("parent")
                        node.name = str(raw.get("name", node.name))
                        node.kind = raw.get("kind", node.kind)
                        node.doc_id = raw.get("doc_id", node.doc_id)
                        node.blob_hash = raw.get("blob_hash", node.blob_hash)
                        node.size = (
                            raw.get("size")
                            if type(raw.get("size")) is int
                            else None
                        )
                        node.required_content_frontier = _decode_content_frontier(
                            raw.get("contentFrontier")
                        )
                        node.ref_size = (
                            raw.get("refSize")
                            if type(raw.get("refSize")) is int
                            else None
                        )
                        node.ref_sha256 = (
                            raw.get("refSha256")
                            if isinstance(raw.get("refSha256"), str)
                            else None
                        )
                        node.deleted = bool(raw.get("deleted", node.deleted))
                    node = space.nodes[node_id]
                    if node.doc_id and node.doc_id not in space.contents:
                        document = _ContentDocument()
                        store = self._store(space_id)
                        if store is not None:
                            try:
                                document = _ContentDocument(
                                    update=store.snapshot(
                                        node.doc_id, shallow_since=None
                                    ).snapshot_bytes
                                )
                            except Exception:
                                pass
                        space.contents[node.doc_id] = document
                for node_id, node in space.nodes.items():
                    current = (
                        node.parent,
                        node.name,
                        node.kind,
                        node.blob_hash,
                        node.deleted,
                    )
                    if previous.get(node_id) == current:
                        continue
                    if node_id not in previous:
                        changed = "created"
                    elif node.deleted and not previous[node_id][4]:
                        changed = "removed"
                    elif (node.parent, node.name) != previous[node_id][:2]:
                        changed = "moved"
                    else:
                        changed = "content"
                    self._finish(
                        space,
                        [node_id],
                        changed,
                        old_paths={node_id: old_paths.get(node_id)},
                        author=origin_author,
                        actor=origin_actor,
                    )
                return
            document = space.contents.setdefault(doc_id, _ContentDocument())
            document.update(update)
            affected = [
                node.node_id for node in space.nodes.values() if node.doc_id == doc_id
            ]
            if affected:
                self._finish(
                    space,
                    affected,
                    "content",
                    author=origin_author,
                    actor=origin_actor,
                )

        refresh()
        if doc_id == "meta":
            self._notify_meta_watchers(space)


    @_facade_locked
    def install_replacement_snapshot(
        self,
        space_id: str,
        old_doc_id: str,
        new_doc_id: str,
        snapshot_bytes: bytes,
    ) -> None:
        """Rebind the in-memory facade after the store installs a G2 snapshot."""

        space = self._space(space_id)
        store = self._store(space_id)
        authority = self.space_authority(space_id, store)
        if authority is None:
            raise OrgFsError(
                "invalid-argument",
                {"message": "replacement snapshot requires a durable authority"},
            )
        active_tree = authority.read(_ReadStore("active_tree_doc_id"))
        if old_doc_id == space.tree_doc_id or active_tree == new_doc_id:
            space.tree_doc_id = new_doc_id
            space.tree = TreeDocument(update=snapshot_bytes)
            previous = space.nodes
            rebuilt: dict[str, _Node] = {}
            for node_id, raw in space.tree.materialize().items():
                old = previous.get(node_id)
                rebuilt[node_id] = _Node(
                    node_id=node_id,
                    parent=raw.get("parent"),
                    name=str(raw.get("name", "")),
                    kind=raw.get("kind", "doc"),
                    doc_id=raw.get("doc_id"),
                    blob_hash=raw.get("blob_hash"),
                    size=(
                        raw.get("size") if type(raw.get("size")) is int else None
                    ),
                    required_content_frontier=_decode_content_frontier(
                        raw.get("contentFrontier")
                    ),
                    ref_size=(
                        raw.get("refSize")
                        if type(raw.get("refSize")) is int
                        else None
                    ),
                    ref_sha256=(
                        raw.get("refSha256")
                        if isinstance(raw.get("refSha256"), str)
                        else None
                    ),
                    deleted=bool(raw.get("deleted", False)),
                    version=old.version if old is not None else "",
                    modified_by=(old.modified_by if old is not None else self.author),
                    modified_via=old.modified_via if old is not None else self.actor,
                )
            space.nodes = rebuilt
        else:
            space.contents[new_doc_id] = _ContentDocument(update=snapshot_bytes)
            space.contents.pop(old_doc_id, None)


    @_facade_locked
    def acknowledge_purge(self, space_id: str, plan_id: str) -> None:
        """Append this member's ack after runtime cleanup has scanned clean."""

        space = self._space(space_id)
        self._forget_history(space)
        acks = space.meta.get("purgeAcks", type=Map)
        current = (acks.to_py() or {}).get(self.node_id)
        if isinstance(current, dict) and plan_id in current:
            return

        def acknowledge() -> None:
            latest = (acks.to_py() or {}).get(self.node_id)
            values = {
                key: Map(value)
                for key, value in (latest.items() if isinstance(latest, dict) else ())
                if isinstance(value, dict)
            }
            values[plan_id] = Map({"ackedAt": _now()})
            acks[self.node_id] = Map(values)

        self._commit(space, "meta", acknowledge)
