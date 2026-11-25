# Decision record (phase F4, real-node finding R2-3, 2026-11-25):
# - Design §9.2 "准入规则的补充": in an org directory space ``move`` refuses
#   any move whose node, old parent or new parent is a protected directory
#   node with ``protected-path`` and the data of every other protected-path
#   refusal (``message``, ``path``), where ``path`` is the path the first such
#   id encodes (node, then old parent, then new parent).  It was a bare
#   ``not-a-member``.  The refusal itself stays: protected ids are derived
#   from the path, so a renamed or moved row would no longer match its id.
from __future__ import annotations








import hashlib








from typing import Callable, Mapping

from hyprial.identity import (
    org_from_space_name,
    parse_protected_directory_node_id,
)






from hyprial.daemon.impl.orgfs.api  import (
    NodeInfo,
    NodeRef,
    OrgFsError)









from hyprial.daemon.impl.orgfs.document.model import _ContentDocument, _NodeSnapshot, _Space, _facade_locked

class FacadeTreeHistory:
    """Responsibility methods on the sole LocalOrgFs state host.

    This class never constructs, copies, or persists an independent host.
    """

    def _would_cycle(self, space: _Space, node_id: str, parent: str | None) -> bool:
        seen: set[str] = set()
        while parent is not None:
            if parent == node_id or parent in seen:
                return True
            seen.add(parent)
            parent = space.nodes.get(parent).parent if parent in space.nodes else None
        return False


    @_facade_locked
    def move(self, space_id: str, source: NodeRef, destination: NodeRef) -> NodeInfo:
        space = self._space(space_id)
        self._ensure_writable(space)
        node = self._one(space, source)
        if node.node_id == "root":
            raise OrgFsError("invalid-argument", {"message": "root cannot move"})
        old_path = self._path(space, node.node_id)
        destination_ids: list[str] = []
        try:
            destination_ids = self._resolve_ids(space, destination)
        except OrgFsError as exc:
            if exc.code != "unknown-doc":
                raise
        if len(destination_ids) > 1:
            raise OrgFsError("ambiguous-path", {"candidates": destination_ids})
        if destination_ids and space.nodes[destination_ids[0]].kind == "dir":
            parent = space.nodes[destination_ids[0]]
            name = node.name
        else:
            parent, name = self._parent_for_new(space, destination)
        if self._would_cycle(space, node.node_id, parent.node_id):
            raise OrgFsError(
                "invalid-argument", {"message": "move would create a cycle"}
            )
        old_parent = node.parent
        if org_from_space_name(space.info.name) is not None:
            try:
                protected = [
                    identity[2]
                    for node_id in (node.node_id, old_parent, parent.node_id)
                    if node_id is not None
                    and (identity := parse_protected_directory_node_id(node_id)) is not None
                ]
            except ValueError as error:
                raise OrgFsError("invalid-argument", {"message": str(error)}) from error
            if protected:
                raise OrgFsError(
                    "protected-path",
                    {"message": f"protected path: {protected[0]}", "path": protected[0]},
                )

        def operation() -> None:
            node.parent = parent.node_id
            node.name = name

        self._write_tree(
            space,
            operation,
            [node.node_id],
            "moved",
            old_paths={node.node_id: old_path},
            move=(node, name, old_parent),
        )
        # Keep the old parent available to debuggers/evidence without adding
        # another mutable field to the public NodeInfo.
        _ = old_parent
        return self._node_info(space, node.node_id)


    @_facade_locked
    def remove(self, space_id: str, node: NodeRef) -> None:
        space = self._space(space_id)
        self._ensure_writable(space)
        item = self._one(space, node)
        if item.node_id == "root":
            raise OrgFsError("invalid-argument", {"message": "root cannot be removed"})
        old_path = self._path(space, item.node_id)
        affected = [item.node_id]
        if item.kind == "dir":
            affected.extend(
                child.node_id
                for child in space.nodes.values()
                if self._is_descendant(space, child.node_id, item.node_id)
            )

        def operation() -> None:
            for node_id in affected:
                space.nodes[node_id].deleted = True

        self._write_tree(
            space, operation, affected, "removed", old_paths={item.node_id: old_path}
        )


    def _is_descendant(self, space: _Space, node_id: str, ancestor: str) -> bool:
        cursor = space.nodes.get(node_id).parent if node_id in space.nodes else None
        seen: set[str] = set()
        while cursor is not None and cursor not in seen:
            if cursor == ancestor:
                return True
            seen.add(cursor)
            cursor = space.nodes.get(cursor).parent if cursor in space.nodes else None
        return False


    @_facade_locked
    def trash(self, space_id: str, limit: int = 100) -> tuple[NodeInfo, ...]:
        space = self._space(space_id)
        removed = [node for node in space.nodes.values() if node.deleted]
        return tuple(
            self._node_info(space, node.node_id) for node in removed[: max(0, limit)]
        )


    @_facade_locked
    def restore(
        self, space_id: str, node: NodeRef, version: str, *, recursive: bool = True
    ) -> NodeInfo:
        space = self._space(space_id)
        self._ensure_writable(space)
        item = self._one(space, node, include_deleted=True)
        snapshots = space.snapshots.get(version)
        if snapshots is None or item.node_id not in snapshots:
            raise OrgFsError("unknown-doc", {"version": version})
        target = snapshots[item.node_id]
        affected = [item.node_id]
        if recursive and target.kind == "dir":
            affected.extend(
                snapshot.node_id
                for snapshot in snapshots.values()
                if snapshot.node_id != item.node_id
                and not snapshot.deleted
                and self._snapshot_descendant(snapshots, snapshot.node_id, item.node_id)
            )
        # Restore missing parent chain before applying the target, as required
        # by the API's no-half-restore contract.
        chain: list[str] = []
        parent = target.parent
        seen_parents: set[str] = set()
        while parent and parent != "root":
            if parent in seen_parents:
                raise OrgFsError(
                    "unknown-doc", {"node": parent, "message": "snapshot parent cycle"}
                )
            seen_parents.add(parent)
            parent_snapshot = snapshots.get(parent)
            if parent_snapshot is None:
                raise OrgFsError("unknown-doc", {"node": parent})
            if parent not in space.nodes or space.nodes[parent].deleted:
                chain.append(parent)
            parent = parent_snapshot.parent
        affected = list(dict.fromkeys(chain + affected))
        for affected_id in affected:
            snap = snapshots.get(affected_id)
            if snap is None:
                continue
            if (
                snap.kind == "blob"
                and snap.blob_hash
                and self.blobs is not None
                and hasattr(self.blobs, "has")
            ):
                if not self.blobs.has(snap.blob_hash):
                    raise OrgFsError("blob-unavailable", {"digest": snap.blob_hash})

        operations: list[tuple[str, Callable[[], None]]] = []
        for affected_id in affected:
            snap = snapshots.get(affected_id)
            if (
                snap is None
                or snap.kind != "doc"
                or not snap.doc_id
                or self._content_doc(space, snap.doc_id, None) is None
            ):
                continue
            restored_text = snap.content.decode(errors="replace")
            document = self._content_doc(space, snap.doc_id)
            if document.value() == restored_text:
                continue

            def restore_content(
                document: _ContentDocument = document,
                restored_text: str = restored_text,
                affected_id: str = affected_id,
            ) -> None:
                document.set(restored_text)
                space.nodes[affected_id].content_frontier += 1

            operations.append((snap.doc_id, restore_content))

        content_restores = {
            affected_id
            for affected_id in affected
            if (snap := snapshots.get(affected_id)) is not None
            and snap.kind == "doc"
            and snap.doc_id is not None
            and self._content_doc(space, snap.doc_id, None) is not None
            and self._content_doc(space, snap.doc_id).value()
            != snap.content.decode(errors="replace")
        }
        restores_content = bool(content_restores)

        tree_changed = any(
            snapshots.get(affected_id) is not None
            and (
                space.nodes[affected_id].parent,
                space.nodes[affected_id].name,
                space.nodes[affected_id].kind,
                space.nodes[affected_id].doc_id,
                space.nodes[affected_id].blob_hash,
                space.nodes[affected_id].size,
                space.nodes[affected_id].deleted,
            )
            != (
                snapshots[affected_id].parent,
                snapshots[affected_id].name,
                snapshots[affected_id].kind,
                snapshots[affected_id].doc_id,
                snapshots[affected_id].blob_hash,
                snapshots[affected_id].size,
                False,
            )
            for affected_id in affected
        )

        def restore_tree() -> None:
            for affected_id in affected:
                snap = snapshots.get(affected_id)
                if snap is None:
                    continue
                restored = space.nodes[affected_id]
                restored.parent = snap.parent
                restored.name = snap.name
                restored.kind = snap.kind
                restored.doc_id = snap.doc_id
                restored.blob_hash = snap.blob_hash
                restored.size = snap.size
                if (
                    restored.kind == "doc"
                    and affected_id in content_restores
                    and restored.doc_id
                ):
                    document = self._content_doc(space, restored.doc_id)
                    raw = document.value().encode()
                    restored.required_content_frontier = document.doc.get_state()
                    restored.ref_size = len(raw)
                    restored.ref_sha256 = hashlib.sha256(raw).hexdigest()
                elif restored.kind == "blob":
                    restored.required_content_frontier = None
                    restored.ref_size = None
                    restored.ref_sha256 = None
                restored.deleted = False
                self._record_row(space, restored)

        if tree_changed or restores_content:
            operations.append((space.tree_doc_id, restore_tree))
        if not operations:
            return self._node_info(space, item.node_id)
        old_path = self._path(space, item.node_id)
        self._commit_many(space, operations)
        self._finish(
            space,
            affected,
            "restored",
            old_paths={item.node_id: old_path},
        )
        return self._node_info(space, item.node_id)


    def _snapshot_descendant(
        self, snapshots: Mapping[str, _NodeSnapshot], node_id: str, ancestor: str
    ) -> bool:
        cursor = snapshots[node_id].parent
        seen: set[str] = set()
        while cursor and cursor not in seen:
            if cursor == ancestor:
                return True
            seen.add(cursor)
            cursor = snapshots.get(cursor).parent if cursor in snapshots else None
        return False
