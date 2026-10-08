from __future__ import annotations





import difflib

import fnmatch


import hashlib








from typing import Callable

from pycrdt import Doc






from hyprial.daemon.impl.orgfs.api  import (
    ChangeEvent,
    NodeInfo,
    NodeRef,
    OrgFsError,
    SpaceStatus,
    TextReadSnapshot)

from hyprial.daemon.impl.orgfs.storage.blobs  import BlobIntegrityError




from hyprial.daemon.impl.orgfs.storage.store  import decode_state_vector, state_covers

from hyprial.daemon.impl.orgfs.storage.space_authority import _ReadStore

from hyprial.daemon.impl.orgfs.document.structured  import StructuredOrgDoc



from hyprial.daemon.impl.orgfs.document.model import ORGFS_TEXT_MAX, _ContentDocument, _Node, _Space, _Watch, _decode_content_frontier, _facade_locked, _version_number

class FacadeContent:
    """Responsibility methods on the sole LocalOrgFs state host.

    This class never constructs, copies, or persists an independent host.
    """

    @_facade_locked
    def stat(self, space_id: str, node: NodeRef) -> NodeInfo:
        space = self._space(space_id)
        return self._node_info(space, self._one(space, node).node_id)


    @_facade_locked
    def listdir(self, space_id: str, node: NodeRef) -> tuple[NodeInfo, ...]:
        space = self._space(space_id)
        parent = self._one(space, node)
        if parent.kind != "dir":
            raise OrgFsError("invalid-argument", {"message": "not a directory"})
        children = sorted(
            (
                child
                for child in space.nodes.values()
                if child.parent == parent.node_id and not child.deleted
                and self._member_row_visible(space, child)
            ),
            key=lambda child: (child.name, child.node_id),
        )
        return tuple(self._node_info(space, child.node_id) for child in children)

    @_facade_locked
    def document_authors(self, space_id: str, node: NodeRef) -> tuple[str, ...]:
        """Return durable content authors for one document node."""

        space = self._space(space_id)
        item = self._one(space, node)
        if item.kind != "doc" or item.doc_id is None:
            raise OrgFsError("invalid-argument", {"message": "not a text document"})
        store = self._store(space_id)
        authority = self.space_authority(space_id, store)
        if authority is not None:
            return tuple(
                authority.read(_ReadStore("document_authors", doc_id=item.doc_id))
            )
        if store is not None and hasattr(store, "document_authors"):
            return tuple(store.document_authors(item.doc_id))
        return tuple(
            sorted({entry.author for entry in space.history.get(item.node_id, ())})
        )


    @_facade_locked
    def document_covers_member_add(
        self, space_id: str, node: NodeRef, user: str
    ) -> bool:
        """Read the document's durable causal relation to the latest member add."""

        space = self._space(space_id)
        item = self._one(space, node)
        if item.kind != "doc" or item.doc_id is None:
            raise OrgFsError("invalid-argument", {"message": "not a text document"})
        store = self._store(space_id)
        authority = self.space_authority(space_id, store)
        if authority is not None:
            return bool(
                authority.read(
                    _ReadStore(
                        "document_covers_member_add",
                        doc_id=item.doc_id,
                        author=user,
                    )
                )
            )
        if store is not None and hasattr(store, "document_covers_member_add"):
            return bool(store.document_covers_member_add(item.doc_id, user))
        return False


    @_facade_locked
    def read_text(self, space_id: str, node: NodeRef) -> tuple[str, str]:
        _space, item, content = self._read_text_value(space_id, node)
        return content, item.version


    @_facade_locked
    def read_text_snapshot(self, space_id: str, node: NodeRef) -> TextReadSnapshot:
        """Return text, version, and canonical identity from one owner snapshot."""

        space, item, content = self._read_text_value(space_id, node)
        return TextReadSnapshot(
            content=content,
            version=item.version,
            node=self._node_info(space, item.node_id),
        )


    def _read_text_value(
        self, space_id: str, node: NodeRef
    ) -> tuple[_Space, _Node, str]:
        space = self._space(space_id)
        item = self._one(space, node)
        if item.kind != "doc" or item.doc_id not in space.contents:
            raise OrgFsError("invalid-argument", {"message": "not a text document"})
        content = space.contents[item.doc_id]
        if (
            item.required_content_frontier is not None
            and not state_covers(
                content.doc.get_state(), item.required_content_frontier
            )
        ):
            raise self._content_pending(space, item)
        return space, item, content.value()


    def _three_way(self, base: str, current: str, requested: str) -> str:
        if current == base:
            return requested
        if requested == base:
            return current
        if current.startswith(base) and requested.startswith(base):
            suffixes = sorted({current[len(base) :], requested[len(base) :]})
            return base + "".join(suffixes)
        if current.endswith(base) and requested.endswith(base):
            prefixes = sorted(
                {
                    current[: -len(base)] if base else current,
                    requested[: -len(base)] if base else requested,
                }
            )
            return "".join(prefixes) + base
        # For overlapping edits, apply the requested diff to the current
        # document.  This retains both edits for disjoint regions and is
        # deterministic when the two edits have identical anchors.
        matcher = difflib.SequenceMatcher(a=base, b=requested, autojunk=False)
        result = current
        delta = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            anchor = min(len(result), i1 + delta)
            result = (
                result[:anchor]
                + requested[j1:j2]
                + result[min(len(result), i2 + delta) :]
            )
            delta += (j2 - j1) - (i2 - i1)
        return result


    @_facade_locked
    def write_text(
        self,
        space_id: str,
        node: NodeRef,
        content: str,
        *,
        base_version: str | None = None,
        expect_version: str | None = None,
        create_only: bool = False,
    ) -> NodeInfo:
        space = self._space(space_id)
        self._ensure_writable(space)
        if len(content.encode()) > ORGFS_TEXT_MAX:
            raise OrgFsError("too-large", {"maxBytes": ORGFS_TEXT_MAX, "use": "blob"})
        existing: _Node | None
        try:
            existing = self._one(space, node)
        except OrgFsError as exc:
            if exc.code != "unknown-doc":
                raise
            existing = None
        if existing is None:
            if base_version is not None or expect_version is not None:
                raise OrgFsError(
                    "stale-write", {"message": "new nodes cannot have a base version"}
                )
            parent, name = self._parent_for_new(space, node)
            created = self._new_node(space, parent, name, "doc")

            created_doc = space.contents[created.doc_id or ""]

            def initialize() -> None:
                created_doc.set(content)

            def record_requirement() -> None:
                created.required_content_frontier = created_doc.doc.get_state()
                raw = content.encode()
                created.ref_size = len(raw)
                created.ref_sha256 = hashlib.sha256(raw).hexdigest()
                space.tree.record(created)

            try:
                self._commit_many(
                    space,
                    [
                        (created.doc_id or "", initialize),
                        (space.tree_doc_id, record_requirement),
                    ],
                )
            except Exception:
                space.nodes.pop(created.node_id, None)
                space.contents.pop(created.doc_id or "", None)
                raise
            self._finish(space, [created.node_id], "created")
            return self._node_info(space, created.node_id)
        if existing.kind != "doc":
            raise OrgFsError(
                "invalid-argument", {"message": "target is not a text document"}
            )
        if create_only:
            raise OrgFsError(
                "stale-write", {"message": "create-only target already exists"}
            )
        if expect_version is not None and existing.version != expect_version:
            raise OrgFsError(
                "stale-write", {"expected": expect_version, "actual": existing.version}
            )
        current_doc = space.contents[existing.doc_id or ""]
        current = current_doc.value()
        if base_version is None:
            base = current
        else:
            snapshot = space.snapshots.get(base_version)
            if snapshot is None or existing.node_id not in snapshot:
                raise OrgFsError("stale-write", {"baseVersion": base_version})
            base = snapshot[existing.node_id].content.decode(errors="replace")
        value = self._three_way(base, current, content)
        if value == current:
            if existing.required_content_frontier is None:
                raw = value.encode()

                def heal_requirement() -> None:
                    existing.required_content_frontier = current_doc.doc.get_state()
                    existing.ref_size = len(raw)
                    existing.ref_sha256 = hashlib.sha256(raw).hexdigest()

                self._write_tree(
                    space,
                    heal_requirement,
                    [existing.node_id],
                    "content",
                )
            return self._node_info(space, existing.node_id)
        old_path = self._path(space, existing.node_id)

        def content_operation() -> None:
            current_doc.set(value)
            existing.content_frontier += 1

        def tree_operation() -> None:
            existing.required_content_frontier = current_doc.doc.get_state()
            raw = value.encode()
            existing.ref_size = len(raw)
            existing.ref_sha256 = hashlib.sha256(raw).hexdigest()
            space.tree.record(existing)

        self._commit_many(
            space,
            [
                (existing.doc_id or "", content_operation),
                (space.tree_doc_id, tree_operation),
            ],
        )
        self._finish(
            space, [existing.node_id], "content", old_paths={existing.node_id: old_path}
        )
        return self._node_info(space, existing.node_id)


    @_facade_locked
    def read_bytes(self, space_id: str, node: NodeRef) -> bytes:
        space = self._space(space_id)
        item = self._one(space, node)
        if item.kind == "doc":
            content = space.contents[item.doc_id or ""]
            if (
                item.required_content_frontier is not None
                and not state_covers(
                    content.doc.get_state(), item.required_content_frontier
                )
            ):
                raise self._content_pending(space, item)
            return content.value().encode()
        if item.kind != "blob" or not item.blob_hash:
            raise OrgFsError("invalid-argument", {"message": "not a file"})
        if self.blobs is None:
            raise self._content_pending(space, item)
        try:
            return self.blobs.get(space_id, item.blob_hash)
        except BlobIntegrityError as exc:
            raise self._content_pending(space, item, local_state="corrupt") from exc
        except Exception as exc:
            if getattr(exc, "code", None) == "purged":
                raise OrgFsError(
                    "purged", dict(getattr(exc, "details", {}) or {})
                ) from exc
            raise self._content_pending(space, item) from exc


    @_facade_locked
    def write_bytes(
        self,
        space_id: str,
        node: NodeRef,
        content: bytes,
        *,
        expect_version: str | None = None,
    ) -> NodeInfo:
        space = self._space(space_id)
        self._ensure_writable(space)
        try:
            existing = self._one(space, node)
        except OrgFsError as exc:
            if exc.code != "unknown-doc":
                raise
            existing = None
        if existing is None and expect_version is not None:
            raise OrgFsError(
                "stale-write", {"message": "new nodes cannot have an expected version"}
            )
        if (
            existing is not None
            and expect_version is not None
            and existing.version != expect_version
        ):
            raise OrgFsError(
                "stale-write", {"expected": expect_version, "actual": existing.version}
            )
        digest = hashlib.sha256(content).hexdigest()
        if self.blobs is not None and hasattr(self.blobs, "put"):
            try:
                digest = self.blobs.put(space_id, content, reason="own-write") or digest
            except Exception as exc:
                if getattr(exc, "code", None) == "purged":
                    raise OrgFsError(
                        "purged", dict(getattr(exc, "details", {}) or {})
                    ) from exc
                raise
        created_new = existing is None
        if existing is None:
            parent, name = self._parent_for_new(space, node)
            existing = self._new_node(space, parent, name, "blob")
            affected = [existing.node_id]
            changed = "created"
        else:
            affected = [existing.node_id]
            changed = "content"
        old_path = self._path(space, existing.node_id)

        def operation() -> None:
            existing.kind = "blob"
            existing.doc_id = None
            existing.blob_hash = digest
            existing.size = len(content)
            existing.required_content_frontier = None
            existing.ref_size = None
            existing.ref_sha256 = None
            existing.deleted = False

        try:
            self._write_tree(
                space,
                operation,
                affected,
                changed,
                old_paths={existing.node_id: old_path},
            )
        except Exception:
            if created_new:
                space.nodes.pop(existing.node_id, None)
            raise
        return self._node_info(space, existing.node_id)


    @_facade_locked
    def mkdir(self, space_id: str, path: str) -> NodeInfo:
        space = self._space(space_id)
        self._ensure_writable(space)
        try:
            self._one(space, path)
        except OrgFsError as exc:
            if exc.code != "unknown-doc":
                raise
        else:
            raise OrgFsError("invalid-argument", {"message": "node already exists"})
        parent, name = self._parent_for_new(space, path)
        node = self._new_node(space, parent, name, "dir")
        try:
            self._write_tree(space, lambda: None, [node.node_id], "created")
        except Exception:
            space.nodes.pop(node.node_id, None)
            raise
        return self._node_info(space, node.node_id)


    @_facade_locked
    def watch(
        self, space_id: str, glob: str, callback: Callable[[ChangeEvent], None]
    ) -> _Watch:
        watcher = _Watch(glob, callback)
        self._space(space_id).watches.append(watcher)
        with self._lock:
            self._watchers[watcher.watch_id] = watcher
        return watcher


    @_facade_locked
    def changes_since(
        self, space_id: str, glob: str = "*", since_version: str | None = None
    ) -> tuple[ChangeEvent, ...]:
        """Return the polling watch projection in commit order."""

        self._space(space_id)
        cutoff = _version_number(since_version) if since_version is not None else -1
        with self._lock:
            events = tuple(self._events.get(space_id, ()))
        return tuple(
            event
            for event in events
            if _version_number(event.node.version) > cutoff
            and fnmatch.fnmatch(event.node.path, glob)
        )


    @_facade_locked
    def open_doc(self, space_id: str, node: NodeRef) -> StructuredOrgDoc:
        """Open a container view with an explicit direct-root deletion contract.

        Structured tombstones hide a deleted P1 direct root from the structured
        projection.  P1 reads then observe that cleared root and a later P1
        write changes its bytes without clearing the tombstone; a structured
        ``set``/``ensure_*`` is the explicit operation that restores visibility.
        """

        space = self._space(space_id)
        item = self._one(space, node)
        if item.kind != "doc" or item.doc_id not in space.contents:
            raise OrgFsError("invalid-argument", {"message": "not a document"})
        node_id = item.node_id
        doc_id = item.doc_id
        document = space.contents[doc_id]
        handle_ref: list[StructuredOrgDoc] = []

        def commit(mutate: Callable[[Doc], None]) -> NodeInfo:
            # User mutation runs only against a detached copy. The owner sees
            # immutable bytes and fences them against the captured CRDT state.
            with self._space_lock(space_id):
                current_space = self._space(space_id)
                current = self._one(current_space, f"id:{node_id}")
                if current.doc_id != doc_id or doc_id not in current_space.contents:
                    raise OrgFsError(
                        "snapshot-barrier",
                        {"retiredDocId": doc_id, "replacementDocId": current.doc_id},
                    )
                self._ensure_writable(current_space)
                live = current_space.contents[doc_id]
                before = bytes(live.doc.get_state())
                client_id = live.doc.client_id
                working = _ContentDocument(client_id=client_id, update=live.export())
            mutate(working.doc)
            update = bytes(working.doc.get_update(before))
            try:
                return self._apply_structured_update(
                    space_id, node_id, doc_id, client_id, before, update
                )
            finally:
                with self._space_lock(space_id):
                    refreshed = self._space(space_id).contents.get(doc_id)
                    if handle_ref and refreshed is not None:
                        handle_ref[0]._document = refreshed  # noqa: SLF001
                        handle_ref[0]._doc = refreshed.doc  # noqa: SLF001

        def version() -> str:
            with self._space_lock(space_id):
                current_space = self._space(space_id)
                return self._node_info(current_space, node_id).version

        handle = StructuredOrgDoc(document, doc_id, commit, version=version)
        handle_ref.append(handle)
        return handle

    @_facade_locked
    def _apply_structured_update(
        self, space_id: str, node_id: str, doc_id: str, client_id: int,
        base_state: bytes, update: bytes,
    ) -> NodeInfo:
        space = self._space(space_id)
        current = self._one(space, f"id:{node_id}")
        if current.doc_id != doc_id or doc_id not in space.contents:
            raise OrgFsError(
                "snapshot-barrier",
                {"retiredDocId": doc_id, "replacementDocId": current.doc_id},
            )
        self._ensure_writable(space)
        live = space.contents[doc_id]
        # Remote clients retain ordinary CRDT merge semantics. Only a local
        # writer advancing the same client clock would reuse generated IDs.
        if (
            live.doc.client_id != client_id
            or decode_state_vector(live.doc.get_state()).get(client_id, 0)
            != decode_state_vector(base_state).get(client_id, 0)
        ):
            raise OrgFsError(
                "stale-write", {"message": "structured local writer advanced"}
            )

        def apply_content() -> None:
            live.doc.apply_update(update)
            current.content_frontier += 1

        def record_requirement() -> None:
            current.required_content_frontier = live.doc.get_state()
            raw = live.value().encode()
            current.ref_size = len(raw)
            current.ref_sha256 = hashlib.sha256(raw).hexdigest()
            space.tree.record(current)

        self._commit_many(
            space, [(doc_id, apply_content), (space.tree_doc_id, record_requirement)]
        )
        self._finish(space, [node_id], "content")
        return self._node_info(space, node_id)


    @_facade_locked
    def status(self, space_id: str) -> SpaceStatus:
        self._space(space_id)
        store = self._store(space_id)
        unconfirmed = (
            int(store.unconfirmed_commits())
            if store and hasattr(store, "unconfirmed_commits")
            else 0
        )
        holders: tuple[tuple[str, bool], ...] = (
            tuple(store.holders_seen())
            if store and hasattr(store, "holders_seen")
            else ()
        )
        parked: tuple[tuple[str, int], ...] = (
            tuple(store.parked_commits())
            if store and hasattr(store, "parked_commits")
            else ()
        )
        return SpaceStatus(
            space_id,
            unconfirmed,
            tuple(node for node, durable in holders if durable),
            tuple(node for node, _durable in holders),
            parked,
        )


    def export_tree_update(self, space_id: str, state: bytes | None = None) -> bytes:
        return self._space(space_id).tree.get_update(state)


    @_facade_locked
    def apply_tree_update(self, space_id: str, update: bytes) -> None:
        space = self._space(space_id)
        space.tree.apply_update(update)
        materialized = space.tree.materialize()
        for node_id, raw in materialized.items():
            if node_id not in space.nodes:
                space.nodes[node_id] = _Node(
                    node_id=node_id,
                    parent=raw.get("parent"),
                    name=raw.get("name", ""),
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
                    modified_by=self.author,
                )
                if raw.get("doc_id") and raw["doc_id"] not in space.contents:
                    space.contents[raw["doc_id"]] = _ContentDocument()
            else:
                node = space.nodes[node_id]
                node.parent = raw.get("parent")
                node.name = raw.get("name", node.name)
                node.kind = raw.get("kind", node.kind)
                node.doc_id = raw.get("doc_id", node.doc_id)
                node.blob_hash = raw.get("blob_hash", node.blob_hash)
                node.size = (
                    raw.get("size") if type(raw.get("size")) is int else None
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
                node.deleted = raw.get("deleted", node.deleted)


    def export_content_update(self, space_id: str, node: NodeRef) -> bytes:
        space = self._space(space_id)
        item = self._one(space, node)
        if item.kind != "doc":
            raise OrgFsError("invalid-argument")
        return space.contents[item.doc_id or ""].export()


    @_facade_locked
    def hydrate_content_snapshot(
        self, space_id: str, node: NodeRef, snapshot: bytes, *, expected_doc_id: str
    ) -> None:
        """Merge a fetched snapshot only into its captured document incarnation."""

        space = self._space(space_id)
        item = self._one(space, node)
        if item.kind != "doc":
            raise OrgFsError("invalid-argument", {"message": "not a text document"})
        if item.doc_id != expected_doc_id:
            raise OrgFsError(
                "snapshot-barrier",
                {"retiredDocId": expected_doc_id, "replacementDocId": item.doc_id},
            )
        if item.doc_id not in space.contents:
            raise OrgFsError("invalid-argument", {"message": "not a text document"})
        space.contents[item.doc_id].doc.apply_update(snapshot)


    @_facade_locked
    def apply_content_update(
        self, space_id: str, node: NodeRef, update: bytes
    ) -> NodeInfo:
        space = self._space(space_id)
        item = self._one(space, node)
        if item.kind != "doc":
            raise OrgFsError("invalid-argument")
        space.contents[item.doc_id or ""].update(update)
        self._finish(space, [item.node_id], "content")
        return self._node_info(space, item.node_id)
