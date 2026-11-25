# Decision record (phase F4, real-node finding R2-2, 2026-11-25):
# - ``_three_way`` (``write_text(base_version=)``) keeps its fast paths, then
#   merges by lines: the base -> requested hunks are mapped onto ``current``
#   through the base -> current hunks.  The old fallback replayed the
#   base -> requested character opcodes at base offsets shifted only by the
#   requested side's own growth, so any edit of ``current`` that changed a
#   length before a requested hunk moved that hunk and corrupted the text
#   (``aaa\nX\nccY``).  Lines, not characters: character opcodes of two
#   rewrites of one line interleave on shared letters and splice both.
# - Hunks that do not overlap in the base are both kept.  Where both writes
#   changed the same base lines, the requested (later) text replaces the
#   current text of those lines, the rule the fallback always stated ("apply
#   the requested diff to the current document"); a requested insertion at
#   the point where ``current`` inserted goes after it.  A requested hunk that
#   spans several current hunks replaces all of them; two requested hunks that
#   land on one current hunk are joined into one replacement.
from __future__ import annotations






import difflib

import fnmatch


import hashlib








from typing import Callable








from hyprial.daemon.impl.orgfs.api  import (
    ChangeEvent,
    NodeInfo,
    NodeRef,
    OrgFsError,
    SpaceStatus,
    TextReadSnapshot)

from hyprial.daemon.impl.orgfs.storage.blobs  import BlobIntegrityError





from hyprial.daemon.impl.orgfs.storage.space_authority import _ReadStore




from hyprial.daemon.impl.orgfs.document.model import ORGFS_TEXT_MAX, _ContentDocument, _Node, _Space, _Watch, _decode_content_frontier, _facade_locked, _version_number

def _line_changes(base: list[str], other: list[str]) -> list[tuple[int, int, int, int]]:
    matcher = difflib.SequenceMatcher(a=base, b=other, autojunk=False)
    return [
        (i1, i2, j1, j2)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def _to_current(changes: list[tuple[int, int, int, int]], position: int, *, after: bool) -> int:
    offset = 0
    for b1, b2, c1, c2 in changes:
        if b2 < position or (b2 == position and (b1 < b2 or after)):
            offset += (c2 - c1) - (b2 - b1)
        elif b1 < position < b2:
            return c2 if after else c1
        else:
            break
    return position + offset


def _merge_lines(base: str, current: str, requested: str) -> str:
    """Apply the base -> requested line hunks to ``current``."""

    base_lines = base.splitlines(keepends=True)
    current_lines = current.splitlines(keepends=True)
    requested_lines = requested.splitlines(keepends=True)
    ours = _line_changes(base_lines, current_lines)
    spans: list[tuple[int, int, int, int]] = []
    for i1, i2, j1, j2 in _line_changes(base_lines, requested_lines):
        start = _to_current(ours, i1, after=True)
        end = max(start, _to_current(ours, i2, after=i1 == i2))
        if spans and start < spans[-1][1]:
            first_start, first_end, first_j1, _first_j2 = spans[-1]
            spans[-1] = (first_start, max(first_end, end), first_j1, j2)
        else:
            spans.append((start, end, j1, j2))
    merged: list[str] = []
    cursor = 0
    for start, end, j1, j2 in spans:
        merged.extend(current_lines[cursor:start])
        merged.extend(requested_lines[j1:j2])
        cursor = end
    merged.extend(current_lines[cursor:])
    return "".join(merged)


class FacadeContent:
    """Responsibility methods on the sole LocalOrgFs state host.

    This class never constructs, copies, or persists an independent host.
    """

    @_facade_locked
    def stat(self, space_id: str, node: NodeRef) -> NodeInfo:
        space = self._space(space_id)
        return self._node_info(space, self._one(space, node).node_id)


    @_facade_locked
    def listdir(
        self, space_id: str, node: NodeRef, *, recursive: bool = False
    ) -> tuple[NodeInfo, ...]:
        space = self._space(space_id)
        parent = self._one(space, node)
        if parent.kind != "dir":
            raise OrgFsError("invalid-argument", {"message": "not a directory"})
        if type(recursive) is not bool:
            raise OrgFsError(
                "invalid-argument", {"message": "recursive must be a boolean"}
            )
        children = sorted(
            (
                child
                for child in space.nodes.values()
                if not child.deleted
                and (
                    self._is_descendant(space, child.node_id, parent.node_id)
                    if recursive
                    else child.parent == parent.node_id
                )
                and self._member_row_visible(space, child)
            ),
            key=lambda child: (self._path(space, child.node_id), child.node_id),
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
            content_revision=hashlib.sha256(self._content_doc(space, item.doc_id).export()).hexdigest(),
        )


    def _read_text_value(
        self, space_id: str, node: NodeRef
    ) -> tuple[_Space, _Node, str]:
        space = self._space(space_id)
        item = self._one(space, node)
        if (
            item.kind != "doc"
            or self._content_doc(space, item.doc_id, None) is None
        ):
            raise OrgFsError("invalid-argument", {"message": "not a text document"})
        content = self._content_doc(space, item.doc_id)
        if self._content_covered(space, item) is False:
            raise self._content_pending(space, item)
        return space, item, content.value()


    def _base_text(self, space: _Space, node: _Node, base_version: str) -> str:
        """Hook: the text ``base_version`` names for a three-way merge.

        Legacy: the node's entry in the in-memory version snapshot.
        """

        snapshot = space.snapshots.get(base_version)
        if snapshot is None or node.node_id not in snapshot:
            raise OrgFsError("stale-write", {"baseVersion": base_version})
        return snapshot[node.node_id].content.decode(errors="replace")

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
        return _merge_lines(base, current, requested)


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

            created_doc = self._content_doc(space, created.doc_id or "")

            def initialize() -> None:
                created_doc.set(content)

            def record_requirement() -> None:
                created.required_content_frontier = created_doc.doc.get_state()
                raw = content.encode()
                created.ref_size = len(raw)
                created.ref_sha256 = hashlib.sha256(raw).hexdigest()
                self._record_row(space, created)

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
                self._drop_content_doc(space, created.doc_id or "")
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
        current_doc = self._content_doc(space, existing.doc_id or "")
        current = current_doc.value()
        if base_version is None:
            base = current
        else:
            base = self._base_text(space, existing, base_version)
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
            self._record_row(space, existing)

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
            content = self._content_doc(space, item.doc_id or "")
            if self._content_covered(space, item) is False:
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


    @_facade_locked
    def parked_commits(self, space_id: str) -> tuple[tuple[str, int], ...]:
        """Read only the pending table; never scan committed history."""

        self._space(space_id)
        store = self._store(space_id)
        return (
            tuple(store.parked_commits())
            if store is not None and hasattr(store, "parked_commits")
            else ()
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
                if (
                    raw.get("doc_id")
                    and self._content_doc(space, raw["doc_id"], None) is None
                ):
                    self._put_content_doc(space, raw["doc_id"], _ContentDocument())
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
        return self._content_doc(space, item.doc_id or "").export()


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
        if self._content_doc(space, item.doc_id, None) is None:
            raise OrgFsError("invalid-argument", {"message": "not a text document"})
        self._content_doc(space, item.doc_id).doc.apply_update(snapshot)
        pending = space.birth_pending.get(item.node_id)
        if (
            pending is not None
            and not self._remote_birth_content_pending(space, item)
        ):
            # Snapshot hydration is the other way a held-back remote birth's
            # content can arrive; finalize it with the origin's attribution.
            space.birth_pending.pop(item.node_id, None)
            self._finish(
                space, [item.node_id], "created", author=pending[0], actor=pending[1]
            )


    @_facade_locked
    def apply_content_update(
        self, space_id: str, node: NodeRef, update: bytes
    ) -> NodeInfo:
        space = self._space(space_id)
        item = self._one(space, node)
        if item.kind != "doc":
            raise OrgFsError("invalid-argument")
        self._content_doc(space, item.doc_id or "").update(update)
        self._finish(space, [item.node_id], "content")
        return self._node_info(space, item.node_id)
