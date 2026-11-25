"""Facade projection and committed state."""
from __future__ import annotations

import fnmatch
import uuid
from typing import Iterable, Mapping


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
    protected_directory_author_allowed,
    protected_directory_node_id,
    protected_directory_doc_id,
)

from hyprial.daemon.impl.orgfs.api  import (
    ChangeEvent,
    NodeInfo,
    NodeKind,
    NodeRef,
    OrgFsError)
from hyprial.daemon.impl.orgfs.document.model import _MetaWatchNotification, _Node, _Space, _WatchNotification, _encode_content_frontier, _facade_locked, _raise_invalid



class FacadeProjection:
    """Responsibility methods on the sole LocalOrgFs state host.

    This class never constructs, copies, or persists an independent host.
    """


    def _notify_meta_watchers(self, space: _Space) -> None:
        for watcher in tuple(space.meta_watches):
            if not watcher.closed:
                self._defer_effect(_MetaWatchNotification(watcher.watch_id))


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
            if node_id in space.nodes:
                if not self._member_row_visible(space, space.nodes[node_id]) or (
                    space.nodes[node_id].deleted and not include_deleted
                ):
                    raise OrgFsError("unknown-doc", {"node": path})
                return [node_id]
            anchor, separator, descendant = node_id.partition("/")
            if not separator or anchor not in space.nodes:
                raise OrgFsError("unknown-doc", {"node": path})
            root = space.nodes[anchor]
            if (
                root.kind != "dir"
                or not self._member_row_visible(space, root)
                or (root.deleted and not include_deleted)
            ):
                raise OrgFsError("unknown-doc", {"node": path})
            candidates = [anchor]
            for part in self._validate_path(descendant):
                candidates = sorted(
                    {
                        child.node_id
                        for child in space.nodes.values()
                        if child.parent in candidates
                        and child.name == part
                        and self._member_row_visible(space, child)
                        and (include_deleted or not child.deleted)
                    }
                )
                if not candidates:
                    raise OrgFsError("unknown-doc", {"node": path})
            return candidates
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
        return any(
            protected_directory_author_allowed(
                section=parts[1], path_principal=identity[1], author=member
            )
            for member in space.members
        )

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


    def _content_pending(
        self,
        space: _Space,
        node: _Node,
        *,
        local_state: str | None = None,
    ) -> OrgFsError:
        required = node.required_content_frontier
        holders = self._holders_hint(space, node)
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
            content = self._content_doc(space, node.doc_id or "", None)
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
        unique = list(dict.fromkeys(affected))
        self._stamp_versions(
            space, unique, changed, attributed_author, attributed_actor
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
        if path.startswith("id:"):
            anchor, separator, descendant = path[3:].partition("/")
            if not separator:
                raise OrgFsError("unknown-doc", {"node": path})
            parts = self._validate_path(descendant)
            parent_ref = f"id:{anchor}"
            if len(parts) > 1:
                parent_ref += "/" + "/".join(parts[:-1])
            parent = self._one(space, parent_ref)
            if parent.kind != "dir" or parent.deleted:
                raise OrgFsError(
                    "invalid-argument", {"message": "parent is not a directory"}
                )
            return parent, parts[-1]
        if path.startswith(ORGFS_URI_PREFIX):
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
        if doc_id and self._content_doc(space, doc_id, None) is None:
            self._new_content_doc(space, node_id, doc_id)
        return node

