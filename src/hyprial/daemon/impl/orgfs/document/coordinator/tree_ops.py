# Decision record (phase D, package D1, 2026-11-25):
# - Design §1.1 / §9 R3: ``NodeInfo.version`` is a digest of the node's
#   materialized tree row (parent and name after move replay, the row's
#   ``contentFrontier`` in canonical encoding, ``author``, ``via``,
#   ``deletedBatch``).  The row's frontier stands for the content vector for
#   loaded and unloaded documents alike, so evicting a document never changes
#   a version and two nodes with the same tree state agree.  No in-process
#   counter enters it, so it is stable across reopen.  A local content write
#   always rewrites the row's frontier, so every local write changes it.
# - Design §9 R1: ``base_version`` merges read a bounded cache filled when a
#   version is handed out (write results and text reads).  A miss is
#   ``stale-write`` unless ``base_version`` is the node's current version, in
#   which case the current text is the base (a plain overwrite).
# - Design §9 R2: ``changes_since`` accepts a cursor ``evt:<process>:<seq>``
#   from ``change_cursor``; any other value is read as a node version and the
#   events after the last event carrying it are returned; an unknown value
#   returns every event (a restart starts over).
# - Design §9 R3: ``_node_info`` never loads a content document: an unloaded
#   document reports the row's ``refSize`` and ``pending`` only when the
#   coordinator already knows it is behind.  An unverified document reports
#   ``arrived`` (reported as a design issue in the D1 report).
# - Design §6, plan §2.7: soft removal gives one fresh ``deletedBatch`` to the
#   rows that this removal turns from live to deleted only; ``restore`` brings
#   back the target, its descendants of the same batch when recursive, and the
#   deleted parent chain; it takes no ``version`` (D4b, design §1.1: the
#   legacy positional version left the surface).  Permanent removal deletes the keys of the node and every
#   descendant and their content documents in one batch, then releases blob
#   references no remaining row retains (§9 R13).
# - D2: a ``content-pending`` error suggests the runtime's known holders
#   (online announced holders first, as the legacy runtime ordered them)
#   followed by the nodes with a current pull observation.
# - F1 (T2 Q-1 ruling): an unloaded document's ``content_state`` is
#   ``pending`` when it is known pending or is a remote birth still waiting
#   for content (``space.birth_pending``, in memory, filled without reading
#   disk); otherwise ``arrived``, which after a restart covers unverified
#   documents until verification classifies them (§9.1 §6-3).
# Decision record (phase D, package F3, 2026-11-25):
# - Plain paths, same-name checks and doc-id lookups read the registry's
#   ``NodeIndex`` instead of scanning ``space.nodes`` (F3 profile: these scans
#   made every write linear in the space).  ``_resolve_ids`` takes over plain
#   paths only (``id:`` and URIs keep the shared reader), with the shared
#   reader's rules: sorted unique candidates, ``_member_row_visible``,
#   ``include_deleted``.  ``_name_conflict`` takes over only when it counts
#   over ``space.nodes`` and also counts the queried node itself, so a node
#   changed inside the running operation before its row is recorded is still
#   seen.  Without a usable registry both fall back to the shared scan.
# - A new node and a recorded row are registered in the index (``note``)
#   instead of dropping it.
# - Package F7 (ruling 5): while this node's author is not a current member,
#   a read, export or wait that needs content not held locally fails with
#   ``not-a-member`` (no ``suggestedHolders``, no content request) instead of
#   ``content-pending``: no member serves it content.  Content already held
#   locally stays readable.
"""Versions, reads, removal and restore of the state host."""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import uuid
from typing import Any, Mapping

from hyprial.kernel import ORGFS_URI_PREFIX
from hyprial.daemon.impl.orgfs.api import (
    ChangeEvent,
    NodeInfo,
    NodeRef,
    OrgFsError,
    TextReadSnapshot,
)
from hyprial.daemon.impl.orgfs.document.coordinator.commit import row_value
from hyprial.daemon.impl.orgfs.document.coordinator.host import state_command
from hyprial.daemon.impl.orgfs.document.coordinator.registry import DocRegistry, NodeIndex
from hyprial.daemon.impl.orgfs.document.model import (
    _ContentDocument,
    _Node,
    _Space,
    _decode_content_frontier,
    _facade_locked,
    _node_uri,
)
from hyprial.daemon.impl.orgfs.membership import (
    LocalWrite,
    check_local_write,
    decode_vector,
    encode_vector,
    row_author,
    row_deleted_batch,
    row_via,
    vector_covers,
    vector_digest,
)

_REQUIRED: Any = object()

CURSOR_PREFIX = "evt:"


def canonical_frontier(value: bytes | None) -> str | None:
    """Canonical base64 of a state vector, or the raw value when malformed."""

    if value is None:
        return None
    try:
        return base64.b64encode(encode_vector(decode_vector(value))).decode("ascii")
    except ValueError:
        return base64.b64encode(value).decode("ascii")


def _int_or_none(value: object) -> int | None:
    return value if type(value) is int else None


class StateTreeOps:
    """Version, read, trash and restore methods of ``LocalOrgFs``."""

    def _sync_nodes(self, space: _Space, registry: DocRegistry) -> tuple[list[str], list[str]]:
        """Bring ``space.nodes`` in line with the tree; return added and vanished ids."""

        placed = space.tree.materialize()
        added: list[str] = []
        batches: dict[str, str] = {}
        for node_id, raw in placed.items():
            fields: dict[str, Any] = {
                "parent": raw.get("parent"),
                "name": str(raw.get("name", "")),
                "kind": raw.get("kind", "doc"),
                "doc_id": raw.get("doc_id"),
                "blob_hash": raw.get("blob_hash"),
                "size": _int_or_none(raw.get("size")),
                "required_content_frontier": _decode_content_frontier(raw.get("contentFrontier")),
                "ref_size": _int_or_none(raw.get("refSize")),
                "ref_sha256": raw.get("refSha256") if isinstance(raw.get("refSha256"), str) else None,
                "deleted": bool(raw.get("deleted", False)),
                "modified_by": row_author(raw) or "",
                "modified_via": row_via(raw),
            }
            node = space.nodes.get(node_id)
            if node is None:
                space.nodes[node_id] = _Node(node_id=node_id, **fields)
                added.append(node_id)
            else:
                for key, value in fields.items():
                    setattr(node, key, value)
            batch = row_deleted_batch(raw)
            if batch is not None:
                batches[node_id] = batch
        vanished = [node_id for node_id in space.nodes if node_id not in placed and node_id != "root"]
        if "root" not in placed and "root" not in space.nodes:
            space.nodes["root"] = _Node("root", None, "", "dir", modified_by=space.info.owner)
        registry.batches = batches
        registry.doc_index = None
        return added, vanished

    def _rematerialize(self, space: _Space) -> None:
        registry = self._registry(space.info.space_id)
        _added, vanished = self._sync_nodes(space, registry)
        for node_id in vanished:
            space.nodes.pop(node_id, None)
        self._refresh_members(space)
        self._refresh_versions(space, list(space.nodes))

    @staticmethod
    def _bound_index(space: _Space, registry: DocRegistry) -> NodeIndex:
        index = registry.doc_index
        if index is None or index.nodes is not space.nodes:
            index = NodeIndex(space.nodes)
            registry.doc_index = index
        return index

    def _live_index(self, space: _Space) -> NodeIndex | None:
        with self._lock:
            registry = self._registries.get(space.info.space_id)
        if registry is None or registry.broken:
            return None
        return self._bound_index(space, registry)

    def _doc_node(self, space: _Space, doc_id: str) -> _Node | None:
        registry = self._registry(space.info.space_id)
        return self._bound_index(space, registry).doc_node(doc_id)

    def _resolve_ids(
        self, space: _Space, path: str, *, include_deleted: bool = False
    ) -> list[str]:
        index = self._live_index(space)
        if index is None or path.startswith(ORGFS_URI_PREFIX) or path.startswith("id:"):
            return super()._resolve_ids(space, path, include_deleted=include_deleted)
        candidates = ["root"]
        for part in self._validate_path(path):
            next_ids: list[str] = []
            for parent in candidates:
                next_ids.extend(
                    node.node_id
                    for node in index.children(parent, part)
                    if self._member_row_visible(space, node)
                    and (include_deleted or not node.deleted)
                )
            candidates = sorted(set(next_ids))
            if not candidates:
                raise OrgFsError("unknown-doc", {"node": path})
        return candidates

    def _name_conflict(
        self, space: _Space, node: _Node, nodes: Mapping[str, _Node] | None = None
    ) -> bool:
        index = self._live_index(space)
        if index is None or (nodes or space.nodes) is not space.nodes:
            return super()._name_conflict(space, node, nodes)
        same = {other.node_id for other in index.children(node.parent, node.name) if not other.deleted}
        current = space.nodes.get(node.node_id)
        if (
            current is not None
            and current.parent == node.parent
            and current.name == node.name
            and not current.deleted
        ):
            same.add(node.node_id)
        return len(same) > 1

    def _new_node(self, space: _Space, parent: _Node, name: str, kind: Any) -> _Node:
        node = super()._new_node(space, parent, name, kind)
        self._note_node(space, node)
        return node

    def _note_node(self, space: _Space, node: _Node | None) -> None:
        with self._lock:
            registry = self._registries.get(space.info.space_id)
        if registry is None or registry.doc_index is None or node is None:
            return
        if registry.doc_index.nodes is space.nodes:
            registry.doc_index.note(node)
        else:
            registry.doc_index = None

    def _node_version(self, space: _Space, node: _Node) -> str:
        registry = self._registry(space.info.space_id)
        value = row_value(node, registry.batches.get(node.node_id) if node.deleted else None)
        value["contentFrontier"] = canonical_frontier(node.required_content_frontier)
        value["author"] = node.modified_by
        value["via"] = node.modified_via
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(hashlib.sha256(encoded).digest()[:18]).decode("ascii")

    def _stamp_versions(
        self,
        space: _Space,
        node_ids: list[str],
        changed: str,
        author: str,
        actor: str | None,
    ) -> None:
        """Hook: versions and attribution come from the rows (design §3.3, §9 R9)."""

        registry = self._registry(space.info.space_id)
        for node_id in node_ids:
            node = space.nodes.get(node_id)
            if node is None:
                continue
            row = space.tree.row(node_id)
            if row is not None:
                node.modified_by = row_author(row) or ""
                node.modified_via = row_via(row)
            node.version = self._node_version(space, node)
            if node.kind == "doc" and node.doc_id:
                document = registry.loaded(node.doc_id)
                if document is not None:
                    registry.base_texts.remember(node_id, node.version, document.value())

    def _refresh_versions(self, space: _Space, node_ids: list[str]) -> None:
        self._stamp_versions(space, node_ids, "", "", None)

    def _base_text(self, space: _Space, node: _Node, base_version: str) -> str:
        """Hook: the text a ``base_version`` names (design §9 R1)."""

        registry = self._registry(space.info.space_id)
        text = registry.base_texts.get(node.node_id, base_version)
        if text is None and base_version == node.version and node.doc_id:
            text = self._content_doc(space, node.doc_id).value()
        if text is None:
            raise OrgFsError("stale-write", {"baseVersion": base_version})
        return text

    def _read_text_value(self, space_id: str, node: NodeRef) -> tuple[_Space, _Node, str]:
        space, item, content = super()._read_text_value(space_id, node)
        self._registry(space_id).base_texts.remember(item.node_id, item.version, content)
        return space, item, content

    @_facade_locked
    def read_text_snapshot(self, space_id: str, node: NodeRef) -> TextReadSnapshot:
        """Text, version and the content vector digest (design §1.1)."""

        space, item, content = self._read_text_value(space_id, node)
        document = self._content_doc(space, item.doc_id or "")
        return TextReadSnapshot(
            content=content,
            version=item.version,
            node=self._node_info(space, item.node_id),
            content_revision=vector_digest(document.doc.get_state()),
        )

    def _content_doc(
        self, space: _Space, doc_id: str, default: Any = _REQUIRED
    ) -> _ContentDocument | None:
        """Hook: load (and pin) a content document the tree references."""

        registry = self._registry(space.info.space_id)
        if doc_id and (registry.loaded(doc_id) is not None or self._doc_node(space, doc_id) is not None):
            return registry.content(doc_id)
        if default is _REQUIRED:
            raise KeyError(doc_id)
        return default

    def _new_content_doc(self, space: _Space, node_id: str, doc_id: str) -> None:
        registry = self._registry(space.info.space_id)
        registry.create_content(doc_id)
        self._note_node(space, space.nodes.get(node_id))

    def _put_content_doc(self, space: _Space, doc_id: str, document: _ContentDocument) -> None:
        self._registry(space.info.space_id).put_content(doc_id, document)

    def _drop_content_doc(self, space: _Space, doc_id: str) -> None:
        registry = self._registry(space.info.space_id)
        registry.drop(doc_id)
        registry.doc_index = None

    def _content_covered(self, space: _Space, node: _Node) -> bool | None:
        if node.required_content_frontier is None or not node.doc_id:
            return None
        document = self._content_doc(space, node.doc_id)
        covered = vector_covers(document.doc.get_state(), node.required_content_frontier)
        self._registry(space.info.space_id).mark(node.doc_id, covered)
        return covered

    def _holders_hint(self, space: _Space, node: _Node) -> list[str]:
        with self._lock:
            sync = self._syncs.get(space.info.space_id)
        hints: dict[str, None] = {}
        if self.mesh is not None and hasattr(self.mesh, "known_holders"):
            try:
                hints.update(dict.fromkeys(self.mesh.known_holders(space.info.space_id)))
            except Exception:
                pass
        if sync is not None and hasattr(sync, "observations"):
            try:
                hints.update(dict.fromkeys(sorted({item.node for item in sync.observations()})))
            except Exception:
                pass
        return list(hints)

    def _content_pending(
        self, space: _Space, node: _Node, *, local_state: str | None = None
    ) -> OrgFsError:
        if self.author not in space.members:
            return OrgFsError("not-a-member", {
                "spaceId": space.info.space_id,
                "node": f"id:{node.node_id}",
                "path": self._path(space, node.node_id),
                "message": "this node's author is not a current member; content it does not hold is not served",
            })
        error = super()._content_pending(space, node, local_state=local_state)
        if node.kind == "doc" and node.doc_id:
            with self._lock:
                sync = self._syncs.get(space.info.space_id)
            if sync is not None and hasattr(sync, "request_content"):
                try:
                    sync.request_content(node.doc_id)
                except Exception as exc:
                    self._log("warn", "orgfs.content.request-failed", docId=node.doc_id, error=str(exc))
        return error

    def _node_info(
        self, space: _Space, node_id: str, nodes: Mapping[str, _Node] | None = None
    ) -> NodeInfo:
        source = nodes or space.nodes
        node = source[node_id]
        registry = self._registry(space.info.space_id)
        size: int | None = None
        content_state: str | None = None
        if node.kind == "doc" and node.doc_id:
            document = registry.loaded(node.doc_id)
            if document is not None:
                size = len(document.value().encode())
                frontier = node.required_content_frontier
                covered = frontier is None or vector_covers(document.doc.get_state(), frontier)
                content_state = "arrived" if covered else "pending"
            else:
                size = node.ref_size
                known_behind = node.doc_id in registry.pending or node_id in space.birth_pending
                content_state = "pending" if known_behind else "arrived"
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

    def change_cursor(self, space_id: str) -> str:
        """An opaque ``changes_since`` cursor for the events seen so far."""

        self._space(space_id)
        with self._lock:
            count = len(self._events.get(space_id, ()))
        return f"{CURSOR_PREFIX}{self._process_token}:{count}"

    def _cursor_start(self, events: tuple[ChangeEvent, ...], since: str) -> int:
        prefix = f"{CURSOR_PREFIX}{self._process_token}:"
        if since.startswith(prefix):
            try:
                return max(0, min(len(events), int(since[len(prefix) :])))
            except ValueError:
                return 0
        for index in range(len(events) - 1, -1, -1):
            if events[index].node.version == since:
                return index + 1
        return 0

    @_facade_locked
    def changes_since(
        self, space_id: str, glob: str = "*", since_version: str | None = None
    ) -> tuple[ChangeEvent, ...]:
        """The polling watch projection after a cursor or a version (design §9 R2)."""

        self._space(space_id)
        with self._lock:
            events = tuple(self._events.get(space_id, ()))
        start = self._cursor_start(events, since_version) if since_version is not None else 0
        return tuple(event for event in events[start:] if fnmatch.fnmatch(event.node.path, glob))

    @state_command
    def remove(self, space_id: str, node: NodeRef, *, permanent: bool = False) -> None:
        space = self._space(space_id)
        self._ensure_writable(space)
        if permanent:
            self._remove_permanently(space, node)
            return
        item = self._one(space, node)
        if item.node_id == "root":
            raise OrgFsError("invalid-argument", {"message": "root cannot be removed"})
        registry = self._registry(space_id)
        old_path = self._path(space, item.node_id)
        affected = [item.node_id]
        if item.kind == "dir":
            affected.extend(
                child.node_id
                for child in space.nodes.values()
                if not child.deleted and self._is_descendant(space, child.node_id, item.node_id)
            )
        batch = uuid.uuid4().hex

        def operation() -> None:
            for node_id in affected:
                space.nodes[node_id].deleted = True
                registry.batches[node_id] = batch

        self._write_tree(space, operation, affected, "removed", old_paths={item.node_id: old_path})

    def _remove_permanently(self, space: _Space, node: NodeRef) -> None:
        item = self._one(space, node, include_deleted=True)
        if item.node_id == "root":
            raise OrgFsError("invalid-argument", {"message": "root cannot be removed"})
        space_id = space.info.space_id
        registry = self._registry(space_id)
        targets = [item.node_id] + [
            child.node_id
            for child in space.nodes.values()
            if self._is_descendant(space, child.node_id, item.node_id)
        ]
        old_path = self._path(space, item.node_id)
        view, policy = self._write_view(space)
        check_local_write(
            self.author,
            LocalWrite(
                "tree",
                paths=tuple(self._path(space, node_id) for node_id in targets),
                node_ids=tuple(targets),
                permanent=True,
                node_author=row_author(space.tree.row(item.node_id) or {}),
            ),
            view,
            policy,
        )
        doc_ids = tuple(
            sorted({space.nodes[node_id].doc_id for node_id in targets if space.nodes[node_id].doc_id})
        )
        digests = {space.nodes[node_id].blob_hash for node_id in targets if space.nodes[node_id].blob_hash}
        live = [node_id for node_id in targets if not space.nodes[node_id].deleted]

        def operation() -> None:
            for node_id in targets:
                if node_id in space.tree.nodes:
                    del space.tree.nodes[node_id]

        self._commit_many(space, (("tree", operation),), deletes=doc_ids)
        for node_id in targets:
            space.nodes[node_id].deleted = True
        self._finish(space, live, "removed", old_paths={item.node_id: old_path})
        for node_id in targets:
            space.nodes.pop(node_id, None)
            space.birth_pending.pop(node_id, None)
            registry.batches.pop(node_id, None)
        registry.doc_index = None
        self._release_blobs(space_id, digests)

    def _release_blobs(self, space_id: str, digests: set[str | None]) -> None:
        """Release blob references that no remaining row retains (§9 R13)."""

        if self.blobs is None or not hasattr(self.blobs, "release"):
            return
        retained = set(self.retained_blob_digests(space_id))
        for digest in sorted(digest for digest in digests if digest and digest not in retained):
            try:
                self.blobs.release(space_id, digest)
            except Exception as exc:
                self._log("warn", "orgfs.blob.release-failed", spaceId=space_id, digest=digest, error=str(exc))

    @_facade_locked
    def restore(
        self,
        space_id: str,
        node: NodeRef,
        *,
        recursive: bool = True,
    ) -> NodeInfo:
        """Restore a removed node's last state."""

        space = self._space(space_id)
        self._ensure_writable(space)
        item = self._one(space, node, include_deleted=True)
        registry = self._registry(space_id)
        targets: list[str] = [item.node_id] if item.deleted else []
        batch = registry.batches.get(item.node_id)
        if recursive and item.kind == "dir" and item.deleted and batch:
            targets.extend(
                child.node_id
                for child in space.nodes.values()
                if child.deleted
                and registry.batches.get(child.node_id) == batch
                and self._is_descendant(space, child.node_id, item.node_id)
            )
        chain: list[str] = []
        seen: set[str] = set()
        parent = item.parent
        while parent and parent != "root" and parent not in seen:
            seen.add(parent)
            ancestor = space.nodes.get(parent)
            if ancestor is None:
                break
            if ancestor.deleted:
                chain.append(parent)
            parent = ancestor.parent
        targets = list(dict.fromkeys([*reversed(chain), *targets]))
        if not targets:
            return self._node_info(space, item.node_id)
        for node_id in targets:
            restored = space.nodes[node_id]
            if (
                restored.kind == "blob"
                and restored.blob_hash
                and self.blobs is not None
                and hasattr(self.blobs, "has")
                and not self.blobs.has(restored.blob_hash)
            ):
                raise OrgFsError("blob-unavailable", {"digest": restored.blob_hash})
        old_path = self._path(space, item.node_id)

        def operation() -> None:
            for node_id in targets:
                space.nodes[node_id].deleted = False
                registry.batches.pop(node_id, None)

        self._write_tree(space, operation, targets, "restored", old_paths={item.node_id: old_path})
        return self._node_info(space, item.node_id)


__all__ = ["CURSOR_PREFIX", "StateTreeOps", "canonical_frontier"]
