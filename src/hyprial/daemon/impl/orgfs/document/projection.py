from __future__ import annotations


from dataclasses import replace




import fnmatch


import hashlib






import uuid


from typing import Any, Callable, Iterable, Mapping

from pycrdt import Doc



from hyprial.kernel import ipc_errors

from hyprial.kernel import ORGFS_CONTENT_PENDING

from hyprial.kernel import (
    ORGFS_URI_PREFIX,
    parse_orgfs_uri,
    parse_user_uri)
from hyprial.identity import (
    ProtectedDocIdTooLongError,
    org_from_space_name,
    parse_protected_directory_node_id,
    protected_directory_node_id,
    protected_directory_doc_id,
)

from hyprial.daemon.impl.orgfs.api  import (
    ChangeEvent,
    NodeInfo,
    NodeKind,
    NodeRef,
    OrgFsError)




from hyprial.daemon.impl.orgfs.storage.space_authority  import (
    _CommittedDelta)

from hyprial.daemon.impl.orgfs.storage.store  import _EMPTY_UPDATE, state_covers


from hyprial.daemon.impl.orgfs.storage.store  import CommitRecord


from hyprial.daemon.impl.orgfs.document.model import TreeDocument, _ABSENT_NODE, _BroadcastPending, _ContentDocument, _History, _Node, _NodeSnapshot, _ReconcileReplicaBlobs, _Space, _SpaceSnapshot, _content_fingerprint, _WatchNotification, _encode_content_frontier, _facade_locked, _node_uri, _now, _raise_invalid, _version


def _snapshot_matches(entry: _NodeSnapshot, node: _Node) -> bool:
    """Whether ``entry`` still describes ``node``'s metadata exactly."""

    return (
        entry.parent == node.parent
        and entry.name == node.name
        and entry.kind == node.kind
        and entry.doc_id == node.doc_id
        and entry.blob_hash == node.blob_hash
        and entry.size == node.size
        and entry.required_content_frontier == node.required_content_frontier
        and entry.ref_size == node.ref_size
        and entry.ref_sha256 == node.ref_sha256
        and entry.deleted == node.deleted
        and entry.version == node.version
        and entry.modified_by == node.modified_by
        and entry.modified_via == node.modified_via
    )


class FacadeProjection:
    """Responsibility methods on the sole LocalOrgFs state host.

    This class never constructs, copies, or persists an independent host.
    """

    def _commit(
        self,
        space: _Space,
        doc_id: str,
        operation: Callable[[], None],
        *,
        broadcast: bool = True,
    ) -> None:
        if not hasattr(self._effect_context, "pending"):
            with self._post_commit_scope(space.info.space_id):
                return self._commit(
                    space, doc_id, operation, broadcast=broadcast
                )
        store = self._store(space.info.space_id)
        if store is None:
            operation()
        else:
            rollback = (
                space.tree.get_update(),
                space.meta.get_update(),
                {node_id: replace(node) for node_id, node in space.nodes.items()},
                {
                    doc_id: document.export()
                    for doc_id, document in space.contents.items()
                },
                dict(space.members),
                set(space.removed_members),
            )
            operation()
            if doc_id == "meta":
                update = space.meta.get_update()
            elif doc_id == space.tree_doc_id:
                update = space.tree.get_update()
            else:
                update = space.contents[doc_id].export()

            def mutate(doc: Any) -> None:
                # Lightweight facade tests use a commit recorder with no CRDT
                # object; the production SpaceStore always supplies one.
                if doc is not None:
                    doc.apply_update(update)

            outbox_records: tuple[CommitRecord, ...] = ()
            absorbs: tuple[_CommittedDelta, ...] = ()
            try:
                authority = self.space_authority(space.info.space_id, store)
                if authority is not None:
                    record, outbox_records, absorbs = authority.commit(
                        doc_id,
                        update,
                        author=self.author,
                        actor=self.actor,
                        absorb_since=(
                            None
                            if doc_id in ("meta", space.tree_doc_id)
                            else space.contents[doc_id].doc.get_state()
                        ),
                    )
                elif hasattr(store, "commit_with_outbox"):
                    record, outbox_records = store.commit_with_outbox(
                        doc_id, mutate, author=self.author, actor=self.actor
                    )
                else:
                    record = store.commit(
                        doc_id, mutate, author=self.author, actor=self.actor
                    )
            except Exception as exc:
                tree_update, meta_update, nodes, contents, members, removed = rollback
                space.tree = TreeDocument(update=tree_update)
                restored_meta = Doc()
                restored_meta.apply_update(meta_update)
                space.meta = restored_meta
                space.nodes = nodes
                space.contents = {
                    key: _ContentDocument(update=value)
                    for key, value in contents.items()
                }
                space.members = members
                space.removed_members = removed
                code = getattr(exc, "code", "invalid-argument")
                details = dict(getattr(exc, "details", {}) or {})
                details.setdefault("message", str(exc))
                raise OrgFsError(str(code), details) from exc
            self._absorb_committed_deltas(space, absorbs)
            if hasattr(store, "take_drained"):
                for envelope in store.take_drained():
                    self.apply_envelope(space.info.space_id, envelope)
            space_id = space.info.space_id
            current_records = (
                outbox_records
                if outbox_records
                else ((record,) if isinstance(record, CommitRecord) else ())
            )
            records = self._publication_records(
                space_id, current_records, broadcast=broadcast
            )
            if doc_id == space.tree_doc_id:
                self._defer_effect(_ReconcileReplicaBlobs(space_id))
            if records or (
                not current_records
                and broadcast
                and self._broadcast_is_enabled()
            ):
                self._defer_effect(_BroadcastPending(space_id, records))


    def _commit_many(
        self, space: _Space, operations: Iterable[tuple[str, Callable[[], None]]]
    ) -> None:
        if not hasattr(self._effect_context, "pending"):
            with self._post_commit_scope(space.info.space_id):
                return self._commit_many(space, operations)
        items = tuple(operations)
        if not items:
            return
        store = self._store(space.info.space_id)
        if store is None:
            for _doc_id, operation in items:
                operation()
            return
        rollback = (
            space.tree.get_update(),
            space.meta.get_update(),
            {node_id: replace(node) for node_id, node in space.nodes.items()},
            {doc_id: document.export() for doc_id, document in space.contents.items()},
            dict(space.contents),
            dict(space.members),
            set(space.removed_members),
        )
        try:
            for _doc_id, operation in items:
                operation()
            mutations: list[tuple[str, Callable[[Any], None]]] = []
            authority_edits: list[tuple[str, bytes]] = []
            for doc_id, _operation in items:
                if doc_id == space.tree_doc_id:
                    update = space.tree.get_update()
                elif doc_id == "meta":
                    update = space.meta.get_update()
                else:
                    update = space.contents[doc_id].export()

                def mutate(doc: Any, update: bytes = update) -> None:
                    doc.apply_update(update)

                mutations.append((doc_id, mutate))
                authority_edits.append((doc_id, update))
            if not hasattr(store, "commit_many"):
                raise RuntimeError(
                    "durable store does not support atomic document batches"
                )
            authority = self.space_authority(space.info.space_id, store)
            absorbs: tuple[_CommittedDelta, ...] = ()
            if authority is not None:
                records, outbox_records, absorbs = authority.commit_many(
                    tuple(authority_edits),
                    author=self.author,
                    actor=self.actor,
                    absorb_since={
                        doc_id: space.contents[doc_id].doc.get_state()
                        for doc_id, _operation in items
                        if doc_id not in ("meta", space.tree_doc_id)
                    },
                )
            elif hasattr(store, "commit_many_with_outbox"):
                records, outbox_records = store.commit_many_with_outbox(
                    mutations, author=self.author, actor=self.actor
                )
            else:
                records = store.commit_many(
                    mutations, author=self.author, actor=self.actor
                )
                outbox_records = ()
        except Exception as exc:
            (
                tree_update,
                meta_update,
                nodes,
                contents,
                content_objects,
                members,
                removed,
            ) = rollback
            space.tree = TreeDocument(update=tree_update)
            restored_meta = Doc()
            restored_meta.apply_update(meta_update)
            space.meta = restored_meta
            space.nodes = nodes
            for key, value in contents.items():
                restored = _ContentDocument(update=value)
                original = content_objects[key]
                original.doc = restored.doc
                original.text = restored.text
            space.contents = content_objects
            space.members = members
            space.removed_members = removed
            if hasattr(store, "take_drained"):
                for envelope in store.take_drained():
                    self.apply_envelope(space.info.space_id, envelope)
            code = getattr(exc, "code", "invalid-argument")
            details = dict(getattr(exc, "details", {}) or {})
            details.setdefault("message", str(exc))
            raise OrgFsError(str(code), details) from exc
        self._absorb_committed_deltas(space, absorbs)
        if hasattr(store, "take_drained"):
            for envelope in store.take_drained():
                self.apply_envelope(space.info.space_id, envelope)
        space_id = space.info.space_id
        current_records = (
            outbox_records
            if outbox_records
            else tuple(record for record in records if isinstance(record, CommitRecord))
        )
        exact_records = self._publication_records(
            space_id, current_records, broadcast=True
        )
        if any(doc_id == space.tree_doc_id for doc_id, _operation in items):
            self._defer_effect(_ReconcileReplicaBlobs(space_id))
        if exact_records or (not current_records and self._broadcast_is_enabled()):
            self._defer_effect(_BroadcastPending(space_id, exact_records))


    @staticmethod
    def _absorb_committed_deltas(
        space: _Space, absorbs: Iterable[_CommittedDelta]
    ) -> None:
        """Fold the store's committed content-doc deltas back into the facade.

        The store adds a reserved coverage-clock op under its writer client to
        every commit.  Peers receive it in the envelope, so a content frontier
        a peer records after editing covers it; the writer's own facade must
        hold it too or it can never satisfy that frontier (D4 x D1 seam).
        The authority returns the delta with the commit result, so the facade
        does no raw store I/O.  Runs on the success path only, after the
        commit; the merge is idempotent and rollback is untouched.
        """

        for absorb in absorbs:
            document = space.contents.get(absorb.doc_id)
            if document is None:
                continue
            if absorb.delta and absorb.delta != _EMPTY_UPDATE:
                document.update(absorb.delta)


    def _ensure_writable(self, space: _Space) -> None:
        member = space.members.get(self.author)
        if (
            member is None
            or member.mode != "rw"
            or self.author in space.removed_members
        ):
            raise OrgFsError("not-a-member", {"spaceId": space.info.space_id})


    def _space(self, space_id: str) -> _Space:
        with self._lock:
            try:
                return self._spaces[space_id]
            except KeyError as exc:
                raise OrgFsError("unknown-space", {"spaceId": space_id}) from exc


    def _existing_space(self, space_id: str) -> _Space | None:
        with self._lock:
            return self._spaces.get(space_id)


    def retained_blob_digests(self, space_id: str) -> tuple[str, ...]:
        """Snapshot current and trash blob references for inbound materialization."""

        with self._space_lock(space_id):
            space = self._existing_space(space_id)
            if space is None:
                return ()
            digests = {
                node.blob_hash
                for node in space.nodes.values()
                if node.blob_hash is not None
            }
        return tuple(sorted(digests))


    @staticmethod
    def _validate_path(path: str) -> list[str]:
        if not isinstance(path, str) or not path:
            return [] if path == "" else _raise_invalid("path must not be empty")
        if path.startswith("/") or "\\" in path:
            _raise_invalid("absolute paths are not accepted")
        parts = path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            _raise_invalid("path contains an empty or reserved segment")
        return parts


    def _resolve_ids(
        self, space: _Space, path: str, *, include_deleted: bool = False
    ) -> list[str]:
        if path.startswith(ORGFS_URI_PREFIX):
            # The one URI reader (design §4): every facade NodeRef funnels
            # through here, so this single branch makes the URI accepted at
            # every surface.  A mismatch rejects before any side effect.
            parsed = parse_orgfs_uri(path)
            if parsed is None:
                raise OrgFsError(ipc_errors.ORGFS_INVALID_URI, {"node": path})
            owner, uri_space, node_id = parsed
            if uri_space != space.info.space_id or owner != (
                parse_user_uri(space.info.owner) or ""
            ):
                raise OrgFsError(
                    ipc_errors.ORGFS_CROSS_SPACE_URI,
                    {
                        "node": path,
                        "expectedSpaceId": space.info.space_id,
                        "uriSpaceId": uri_space,
                        "expectedOwner": space.info.owner,
                        "uriOwner": owner,
                    },
                )
            # From here identical to the id:<nodeId> branch.
            if (
                node_id not in space.nodes
                or not self._member_row_visible(space, space.nodes[node_id])
                or (space.nodes[node_id].deleted and not include_deleted)
            ):
                raise OrgFsError("unknown-doc", {"node": path})
            return [node_id]
        if path.startswith("id:"):
            node_id = path[3:]
            if (
                node_id not in space.nodes
                or not self._member_row_visible(space, space.nodes[node_id])
                or (space.nodes[node_id].deleted and not include_deleted)
            ):
                raise OrgFsError("unknown-doc", {"node": path})
            return [node_id]
        parts = self._validate_path(path)
        candidates = ["root"]
        for part in parts:
            next_ids: list[str] = []
            for parent in candidates:
                next_ids.extend(
                    node.node_id
                    for node in space.nodes.values()
                    if node.parent == parent
                    and node.name == part
                    and self._member_row_visible(space, node)
                    and (include_deleted or not node.deleted)
                )
            candidates = sorted(set(next_ids))
            if not candidates:
                raise OrgFsError("unknown-doc", {"node": path})
        return candidates


    @staticmethod
    def _member_row_visible(space: _Space, node: _Node) -> bool:
        """Hide removed members' protected rows using causal org meta state."""

        try:
            identity = parse_protected_directory_node_id(node.node_id)
        except ValueError:
            return False
        if identity is None:
            return True
        parts = identity[2].split("/")
        if len(parts) < 3 or parts[:2] not in (
            ["directory", "devices"],
            ["directory", "people"],
            ["directory", "leaves"],
        ):
            return True
        return identity[1] in space.members


    @_facade_locked
    def resolve(self, space_id: str, path: str) -> tuple[NodeInfo, ...]:
        space = self._space(space_id)
        return tuple(
            self._node_info(space, node_id)
            for node_id in self._resolve_ids(space, path)
        )


    def _one(
        self, space: _Space, node: NodeRef, *, include_deleted: bool = False
    ) -> _Node:
        ids = self._resolve_ids(space, node, include_deleted=include_deleted)
        if len(ids) > 1:
            raise OrgFsError("ambiguous-path", {"candidates": ids})
        return space.nodes[ids[0]]


    def _path(
        self, space: _Space, node_id: str, nodes: Mapping[str, _Node] | None = None
    ) -> str:
        source = nodes or space.nodes
        if node_id == "root":
            return ""
        pieces: list[str] = []
        seen: set[str] = set()
        cursor: str | None = node_id
        while cursor and cursor != "root":
            if cursor in seen or cursor not in source:
                break
            seen.add(cursor)
            item = source[cursor]
            pieces.append(item.name)
            cursor = item.parent
        return "/".join(reversed(pieces))


    def _name_conflict(
        self, space: _Space, node: _Node, nodes: Mapping[str, _Node] | None = None
    ) -> bool:
        source = nodes or space.nodes
        return (
            sum(
                1
                for other in source.values()
                if other.parent == node.parent
                and other.name == node.name
                and not other.deleted
            )
            > 1
        )


    def _node_info(
        self, space: _Space, node_id: str, nodes: Mapping[str, _Node] | None = None
    ) -> NodeInfo:
        source = nodes or space.nodes
        node = source[node_id]
        content = space.contents.get(node.doc_id or "")
        size = None
        content_state = None
        if node.kind == "doc" and content is not None:
            size = len(content.value().encode())
            if node.required_content_frontier is None:
                content_state = "unverifiable"
            else:
                content_state = (
                    "arrived"
                    if state_covers(
                        content.doc.get_state(), node.required_content_frontier
                    )
                    else "pending"
                )
        elif node.kind == "blob" and node.blob_hash:
            size = node.size
            content_state = (
                "arrived"
                if self.blobs is not None
                and (
                    self.blobs.contains(node.blob_hash)
                    if hasattr(self.blobs, "contains")
                    else self.blobs.has(node.blob_hash)
                )
                else "pending"
            )
        return NodeInfo(
            space_id=space.info.space_id,
            node_id=node.node_id,
            kind=node.kind,
            name=node.name,
            path=self._path(space, node_id, source),
            size=size,
            blob_hash=node.blob_hash,
            doc_id=node.doc_id,
            version=node.version,
            modified_by=node.modified_by,
            modified_via=node.modified_via,
            name_conflict=self._name_conflict(space, node, source),
            deleted=node.deleted,
            content_state=content_state,
            uri=_node_uri(space.info, node_id),
        )


    def _content_pending(
        self,
        space: _Space,
        node: _Node,
        *,
        local_state: str | None = None,
    ) -> OrgFsError:
        required = node.required_content_frontier
        holders: list[str] = []
        if self.mesh is not None and hasattr(self.mesh, "known_holders"):
            holders = list(
                self.mesh.known_holders(
                    space.info.space_id,
                    doc_id=node.doc_id if node.kind == "doc" else None,
                    required_frontier=required,
                )
            )
        details: dict[str, object] = {
            "kind": node.kind,
            "spaceId": space.info.space_id,
            "node": f"id:{node.node_id}",
            "path": self._path(space, node.node_id),
            "suggestedHolders": holders,
            "waitedSeconds": 0.0,
        }
        if node.kind == "blob":
            details.update(
                {
                    "expectedSize": node.size,
                    "expectedSha256": node.blob_hash,
                    "localState": local_state or "absent",
                }
            )
        else:
            content = space.contents.get(node.doc_id or "")
            details.update(
                {
                    "requiredFrontier": (
                        _encode_content_frontier(required)
                        if required is not None
                        else None
                    ),
                    "localFrontier": (
                        _encode_content_frontier(content.doc.get_state())
                        if content is not None
                        else _encode_content_frontier(b"\x00")
                    ),
                }
            )
        return OrgFsError(ORGFS_CONTENT_PENDING, details)


    def _forget_history(self, space: _Space) -> None:
        """Drop every earlier in-memory version after a purge.

        Versions share snapshot entries, so a purged text stays readable
        through ``read_at``/``history``/``restore`` for as long as any version
        that saw it is kept.  After a purge the space keeps only its current
        state -- what a restart would leave -- registered under each node's
        current version label, so current-version reads and writes still work.
        """

        space.history.clear()
        space.snapshots.clear()
        space.snapshot_marks.clear()
        current = self._snapshot(space)
        for version in {node.version for node in space.nodes.values()}:
            space.snapshots[version] = current


    def _snapshot(self, space: _Space) -> _SpaceSnapshot:
        """Record the space as a layer over its latest version.

        Only nodes whose metadata or content changed get a new entry (and
        only those encode their text); the rest share the latest version's
        entry, so a write costs O(changed nodes) in kept memory.
        """

        previous = next(reversed(space.snapshots.values()), None)
        if not isinstance(previous, _SpaceSnapshot):
            previous = None
            space.snapshot_marks.clear()
        marks = space.snapshot_marks
        changes: dict[str, Any] = {}
        for node_id, node in space.nodes.items():
            document = space.contents.get(node.doc_id) if node.doc_id else None
            fingerprint = (
                _content_fingerprint(document) if document is not None else None
            )
            mark = marks.get(node_id)
            if previous is not None and mark is not None:
                prior, prior_document, prior_fingerprint = mark
                if (
                    previous.get(node_id) is prior
                    and prior_document is document
                    and prior_fingerprint == fingerprint
                    and _snapshot_matches(prior, node)
                ):
                    continue
            entry = _NodeSnapshot(
                node_id=node.node_id,
                parent=node.parent,
                name=node.name,
                kind=node.kind,
                doc_id=node.doc_id,
                blob_hash=node.blob_hash,
                size=node.size,
                required_content_frontier=node.required_content_frontier,
                ref_size=node.ref_size,
                ref_sha256=node.ref_sha256,
                deleted=node.deleted,
                version=node.version,
                modified_by=node.modified_by,
                modified_via=node.modified_via,
                content=document.value().encode() if document is not None else b"",
            )
            marks[node_id] = (entry, document, fingerprint)
            changes[node_id] = entry
        for node_id in tuple(marks):
            if node_id not in space.nodes:
                del marks[node_id]
                changes[node_id] = _ABSENT_NODE
        return _SpaceSnapshot(previous, changes)


    def _finish(
        self,
        space: _Space,
        affected: Iterable[str],
        changed: str,
        *,
        old_paths: Mapping[str, str | None] | None = None,
        author: str | None = None,
        actor: str | None = None,
    ) -> None:
        attributed_author = self.author if author is None else author
        attributed_actor = self.actor if author is None else actor
        with self._lock:
            self._clock += 1
            clock = self._clock
        space.revision += 1
        version = _version(space.revision, clock)
        at = _now()
        unique = list(dict.fromkeys(affected))
        for node_id in unique:
            node = space.nodes[node_id]
            node.version = version
            node.modified_by = attributed_author
            node.modified_via = attributed_actor
        snapshot = self._snapshot(space)
        space.snapshots[version] = snapshot
        for node_id in unique:
            node = space.nodes[node_id]
            space.history.setdefault(node_id, []).append(
                _History(
                    snapshot[node_id], changed, attributed_author, attributed_actor, at
                )
            )
        for node_id in unique:
            info = self._node_info(space, node_id)
            kind = "modified" if changed in {"content", "both", "position"} else changed
            event = ChangeEvent(
                space.info.space_id, kind, info, (old_paths or {}).get(node_id)
            )
            with self._lock:
                events = self._events.setdefault(space.info.space_id, [])
            events.append(event)
            for watcher in tuple(space.watches):
                if watcher.closed or not fnmatch.fnmatch(info.path, watcher.glob):
                    continue
                watcher.events.append(event)
                self._defer_effect(_WatchNotification(watcher.watch_id, event))


    def _parent_for_new(self, space: _Space, path: str) -> tuple[_Node, str]:
        if path.startswith("id:") or path.startswith(ORGFS_URI_PREFIX):
            # A URI names an existing node; you cannot create by URI.  This
            # guard is essential for mutations: without it a write to the
            # URI of a nonexistent node would CREATE a doc literally named
            # ``orgfs:…`` (dispatcher.md hot-fixer answer 1).
            raise OrgFsError("unknown-doc", {"node": path})
        parts = self._validate_path(path)
        if not parts:
            raise OrgFsError(
                "invalid-argument", {"message": "a child path is required"}
            )
        name = parts[-1]
        parent_path = "/".join(parts[:-1])
        parent = self._one(space, parent_path) if parent_path else space.nodes["root"]
        if parent.kind != "dir" or parent.deleted:
            raise OrgFsError(
                "invalid-argument", {"message": "parent is not a directory"}
            )
        return parent, name


    def _new_node(
        self, space: _Space, parent: _Node, name: str, kind: NodeKind
    ) -> _Node:
        parent_path = self._path(space, parent.node_id)
        path = f"{parent_path}/{name}" if parent_path else name
        doc_id: str | None = None
        try:
            protected_doc = (
                protected_directory_doc_id(
                    space.info.space_id,
                    path,
                    space_owner=space.info.owner,
                )
                if kind == "doc" and org_from_space_name(space.info.name) is not None
                else None
            )
            protected_node = (
                protected_directory_node_id(
                    space.info.space_id,
                    path,
                    space_owner=space.info.owner,
                )
                if org_from_space_name(space.info.name) is not None
                else None
            )
        except ProtectedDocIdTooLongError as error:
            raise OrgFsError(
                "invalid-argument",
                {
                    "message": str(error),
                    "reason": (
                        "protected-doc-id-too-long"
                        if kind == "doc"
                        else "protected-node-id-too-long"
                    ),
                },
            ) from error
        node_id = protected_node or uuid.uuid4().hex
        if kind == "doc":
            doc_id = protected_doc or f"doc-{uuid.uuid4()}"
        node = _Node(
            node_id, parent.node_id, name, kind, doc_id=doc_id, modified_by=self.author
        )
        space.nodes[node_id] = node
        if doc_id and doc_id not in space.contents:
            client_id = int.from_bytes(
                hashlib.sha256(node_id.encode()).digest()[:8], "big"
            ) & ((1 << 53) - 1)
            space.contents[doc_id] = _ContentDocument(client_id=client_id)
        return node


    def _write_tree(
        self,
        space: _Space,
        operation: Callable[[], None],
        affected: Iterable[str],
        changed: str,
        *,
        old_paths: Mapping[str, str | None] | None = None,
        move: tuple[_Node, str, str | None] | None = None,
        broadcast: bool = True,
    ) -> None:
        def apply() -> None:
            operation()
            if move is not None:
                node, name, old_parent = move
                with self._lock:
                    self._clock += 1
                    clock = self._clock
                space.tree.record_move(
                    node,
                    timestamp=clock,
                    peer=self.node_id,
                    old_parent=old_parent,
                    new_parent=node.parent,
                    name=name,
                )
            else:
                for node_id in affected:
                    space.tree.record(space.nodes[node_id])

        self._commit(space, space.tree_doc_id, apply, broadcast=broadcast)
        self._finish(space, affected, changed, old_paths=old_paths)
