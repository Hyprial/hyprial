# Decision record (phase F4, real-node finding R2-3, 2026-11-25):
# - Design §9.2 "准入规则的补充": in an org directory space ``move`` refuses
#   any move whose node, old parent or new parent is a protected directory
#   node with ``protected-path`` and the data of every other protected-path
#   refusal (``message``, ``path``), where ``path`` is the path the first such
#   id encodes (node, then old parent, then new parent).  It was a bare
#   ``not-a-member``.  The refusal itself stays: protected ids are derived
#   from the path, so a renamed or moved row would no longer match its id.
from __future__ import annotations





from hyprial.identity import (
    org_from_space_name,
    parse_protected_directory_node_id,
)


from hyprial.daemon.impl.orgfs.api  import (
    NodeInfo,
    NodeRef,
    OrgFsError)


from hyprial.daemon.impl.orgfs.document.model import _Space, _facade_locked

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
