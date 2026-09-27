"""The local orgfs document and tree engine.

The tree is represented by a pycrdt document containing node metadata and an
append-only move array.  The move array is intentionally separate from the
current parent pointers: replicas replay the same total order and skip a move
which would create a cycle.  That is the small Kleppmann-style movable-tree
core required by P1 and makes concurrent ``A -> B``/``B -> A`` deterministic.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import difflib
import fnmatch
from functools import wraps
import hashlib
import json
from pathlib import Path
import threading
import uuid
from typing import Any, Callable, Iterable, Mapping, Sequence

from pycrdt import Array, Doc, Map, Text

from .api import (
    ChangeEvent,
    HistoryEntry,
    MemberInfo,
    MemberMode,
    NodeInfo,
    NodeKind,
    NodeRef,
    OrgFsError,
    SpaceInfo,
    SpaceStatus,
)
from .purge import (
    PurgeBlob,
    PurgeDocument,
    PurgePlan,
    PurgeResult,
    PurgeSnapshot,
    PurgeStatus,
    parse_iso,
    utc_now,
)
from .replica import encode_snapshot_frontier
from .structured import StructuredOrgDoc

ORGFS_TEXT_MAX = 4 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _facade_locked(method: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize every facade mutation with inbound envelope application."""

    @wraps(method)
    def locked(self: "LocalOrgFs", *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return locked


def _version(tree_frontier: int, content_frontier: int) -> str:
    """Encode a version as an opaque, URL-safe JSON value."""

    raw = json.dumps(
        {"treeFrontier": tree_frontier, "contentFrontier": content_frontier},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _version_number(value: str) -> int:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
        return int(decoded["treeFrontier"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return -1


@dataclass
class _Node:
    node_id: str
    parent: str | None
    name: str
    kind: NodeKind
    doc_id: str | None = None
    blob_hash: str | None = None
    deleted: bool = False
    version: str = ""
    modified_by: str = ""
    modified_via: str | None = None
    content_frontier: int = 0


@dataclass(frozen=True)
class _NodeSnapshot:
    node_id: str
    parent: str | None
    name: str
    kind: NodeKind
    doc_id: str | None
    blob_hash: str | None
    deleted: bool
    version: str
    modified_by: str
    modified_via: str | None
    content: bytes


@dataclass
class _History:
    node: _NodeSnapshot
    changed: str
    author: str
    actor: str | None
    at: str


@dataclass
class _Watch:
    glob: str
    callback: Callable[[ChangeEvent], None]
    events: list[ChangeEvent] = field(default_factory=list)
    closed: bool = False

    def close(self) -> None:
        self.closed = True


class TreeDocument:
    """A pycrdt-backed tree log with deterministic move replay.

    Values in the Y.Map are JSON strings rather than nested Python objects so
    the update stream stays stable across pycrdt releases.  ``moves`` is a
    Y.Array of JSON move records; the projection is pure and therefore safe to
    rebuild after receiving an update from another replica.
    """

    def __init__(
        self, *, client_id: int | None = None, update: bytes | None = None
    ) -> None:
        self.doc = Doc(client_id=client_id)
        if update is not None:
            self.doc.apply_update(update)
        self.nodes = self.doc.get("nodes", type=Map)
        self.moves = self.doc.get("moves", type=Array)
        if "root" not in self.nodes:
            self._write_node(
                {
                    "node_id": "root",
                    "parent": None,
                    "name": "",
                    "kind": "dir",
                    "doc_id": None,
                    "blob_hash": None,
                    "deleted": False,
                }
            )

    @staticmethod
    def _wire_node(node: Mapping[str, Any]) -> str:
        return json.dumps(dict(node), sort_keys=True, separators=(",", ":"))

    def _write_node(self, node: Mapping[str, Any]) -> None:
        self.nodes[str(node["node_id"])] = self._wire_node(node)

    def record_node(self, node: _Node) -> None:
        self._write_node(
            {
                "node_id": node.node_id,
                "parent": node.parent,
                "name": node.name,
                "kind": getattr(node, "kind", "dir"),
                "doc_id": getattr(node, "doc_id", None),
                "blob_hash": getattr(node, "blob_hash", None),
                "deleted": getattr(node, "deleted", False),
            }
        )

    def record_move(
        self,
        node: _Node,
        *,
        timestamp: int,
        peer: str,
        new_parent: str | None,
        name: str,
    ) -> None:
        with self.doc.transaction():
            self.moves.append(
                json.dumps(
                    {
                        "ts": timestamp,
                        "peer": peer,
                        "node": node.node_id,
                        "newParent": new_parent,
                        "name": name,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

    def record(self, node: _Node) -> None:
        with self.doc.transaction():
            self.record_node(node)

    def get_update(self, state: bytes | None = None) -> bytes:
        return self.doc.get_update(state)

    def apply_update(self, update: bytes) -> None:
        self.doc.apply_update(update)

    def _raw_nodes(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for node_id, raw in (self.nodes.to_py() or {}).items():
            if isinstance(raw, str):
                try:
                    result[node_id] = json.loads(raw)
                except json.JSONDecodeError:
                    continue
        return result

    def materialize(self) -> dict[str, dict[str, Any]]:
        nodes = self._raw_nodes()
        moves: list[dict[str, Any]] = []
        for raw in self.moves.to_py() or []:
            if isinstance(raw, str):
                try:
                    moves.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        parents = {key: value.get("parent") for key, value in nodes.items()}
        names = {key: value.get("name", "") for key, value in nodes.items()}

        def cycle(node_id: str, parent: str | None) -> bool:
            seen: set[str] = set()
            cursor = parent
            while cursor is not None:
                if cursor == node_id:
                    return True
                if cursor in seen:
                    return True
                seen.add(cursor)
                cursor = parents.get(cursor)
            return False

        ordered = sorted(
            moves,
            key=lambda item: (
                int(item.get("ts", 0)),
                str(item.get("peer", "")),
                str(item.get("node", "")),
            ),
        )
        for move in ordered:
            node_id = str(move.get("node", ""))
            parent = move.get("newParent")
            if node_id not in nodes or parent is not None and parent not in nodes:
                continue
            if cycle(node_id, parent):
                # The important safety invariant: never materialize a cycle.
                continue
            parents[node_id] = parent
            if "name" in move:
                names[node_id] = str(move["name"])
        for node_id, node in nodes.items():
            node["parent"] = parents.get(node_id)
            node["name"] = names.get(node_id, node.get("name", ""))
        return nodes

    def has_cycle(self) -> bool:
        nodes = self.materialize()
        for node_id, node in nodes.items():
            seen: set[str] = set()
            cursor = node.get("parent")
            while cursor is not None:
                if cursor == node_id or cursor in seen:
                    return True
                seen.add(cursor)
                cursor = nodes.get(cursor, {}).get("parent")
        return False


class _ContentDocument:
    def __init__(
        self,
        text: str = "",
        *,
        client_id: int | None = None,
        update: bytes | None = None,
    ) -> None:
        self.doc = Doc(client_id=client_id)
        if update is not None:
            self.doc.apply_update(update)
        self.text = self.doc.get("text", type=Text)
        if text:
            with self.doc.transaction():
                self.text.insert(0, text)

    def value(self) -> str:
        return self.text.to_py() or ""

    def set(self, value: str) -> None:
        current = self.value()
        if value == current:
            return
        shared_prefix = 0
        prefix_limit = min(len(current), len(value))
        while (
            shared_prefix < prefix_limit
            and current[shared_prefix] == value[shared_prefix]
        ):
            shared_prefix += 1
        shared_suffix = 0
        suffix_limit = min(len(current), len(value)) - shared_prefix
        while (
            shared_suffix < suffix_limit
            and current[len(current) - shared_suffix - 1]
            == value[len(value) - shared_suffix - 1]
        ):
            shared_suffix += 1
        old_stop = len(current) - shared_suffix
        new_stop = len(value) - shared_suffix
        matcher = difflib.SequenceMatcher(
            a=current[shared_prefix:old_stop],
            b=value[shared_prefix:new_stop],
            autojunk=False,
        )
        edits = [
            (
                tag,
                i1 + shared_prefix,
                i2 + shared_prefix,
                j1 + shared_prefix,
                j2 + shared_prefix,
            )
            for tag, i1, i2, j1, j2 in matcher.get_opcodes()
            if tag != "equal"
        ]
        # pycrdt.Text/Yrs positions count UTF-8 bytes, while SequenceMatcher
        # positions count Python Unicode code points.  Resolve only the edit
        # boundaries, walking the old string once even when there are many edits.
        boundaries = sorted(
            {index for _, i1, i2, _, _ in edits for index in (i1, i2)}
        )
        byte_offsets: dict[int, int] = {}
        previous_index = 0
        previous_offset = 0
        for index in boundaries:
            previous_offset += len(current[previous_index:index].encode())
            byte_offsets[index] = previous_offset
            previous_index = index

        with self.doc.transaction():
            # Later ranges cannot disturb the positions of earlier ranges.
            for _tag, i1, i2, j1, j2 in reversed(edits):
                start = byte_offsets[i1]
                stop = byte_offsets[i2]
                if stop > start:
                    del self.text[start:stop]
                if j2 > j1:
                    self.text.insert(start, value[j1:j2])

    def update(self, state: bytes) -> None:
        self.doc.apply_update(state)

    def export(self) -> bytes:
        return self.doc.get_update()


@dataclass
class _Space:
    info: SpaceInfo
    tree_doc_id: str
    tree: TreeDocument
    meta: Doc
    nodes: dict[str, _Node]
    contents: dict[str, _ContentDocument]
    members: dict[str, MemberInfo]
    removed_members: set[str]
    history: dict[str, list[_History]]
    snapshots: dict[str, dict[str, _NodeSnapshot]]
    revision: int = 0
    watches: list[_Watch] = field(default_factory=list)


class _OrgDoc:
    def __init__(self, document: _ContentDocument, doc_id: str) -> None:
        self._document = document
        self._doc_id = doc_id

    def doc_id(self) -> str:
        return self._doc_id

    def snapshot_json(self) -> str:
        return json.dumps(
            {"text": self._document.value()}, ensure_ascii=False, sort_keys=True
        )


class LocalOrgFs:
    """Reference local facade used by the daemon and by isolated tests.

    ``stores`` and ``blobs`` are optional duck-typed injections.  When a
    SpaceStore is supplied every mutation is submitted through its ``commit``
    method; the local fallback is intentionally only an in-process test
    backend and has no filesystem side effects.
    """

    def __init__(
        self,
        stores: Any = None,
        blobs: Any = None,
        mesh: Any = None,
        *,
        author: str = "user:local",
        actor: str | None = None,
        node_id: str = "local",
    ) -> None:
        self.stores = stores
        self.blobs = blobs
        self.mesh = mesh
        self.author = author
        self.actor = actor
        self.node_id = node_id
        self._lock = threading.RLock()
        self._spaces: dict[str, _Space] = {}
        self._clock = 0
        self._events: dict[str, list[ChangeEvent]] = {}
        self._purge_plans: dict[str, tuple[PurgePlan, tuple[dict[str, str], ...]]] = {}

    def _store(self, space_id: str) -> Any:
        if self.stores is None:
            return None
        if hasattr(self.stores, "commit"):
            return self.stores
        if isinstance(self.stores, Mapping):
            return self.stores.get(space_id)
        return None

    def _commit(
        self,
        space: _Space,
        doc_id: str,
        operation: Callable[[], None],
        *,
        broadcast: bool = True,
    ) -> None:
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

            try:
                store.commit(doc_id, mutate, author=self.author, actor=self.actor)
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
            if hasattr(store, "take_drained"):
                for envelope in store.take_drained():
                    self.apply_envelope(space.info.space_id, envelope)
            if (
                broadcast
                and self.mesh is not None
                and hasattr(self.mesh, "broadcast_pending")
            ):
                self.mesh.broadcast_pending(space.info.space_id)

    def _commit_many(
        self, space: _Space, operations: Iterable[tuple[str, Callable[[], None]]]
    ) -> None:
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
            dict(space.members),
            set(space.removed_members),
        )
        try:
            for _doc_id, operation in items:
                operation()
            mutations: list[tuple[str, Callable[[Any], None]]] = []
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
            if not hasattr(store, "commit_many"):
                raise RuntimeError(
                    "durable store does not support atomic document batches"
                )
            store.commit_many(mutations, author=self.author, actor=self.actor)
        except Exception as exc:
            tree_update, meta_update, nodes, contents, members, removed = rollback
            space.tree = TreeDocument(update=tree_update)
            restored_meta = Doc()
            restored_meta.apply_update(meta_update)
            space.meta = restored_meta
            space.nodes = nodes
            space.contents = {
                key: _ContentDocument(update=value) for key, value in contents.items()
            }
            space.members = members
            space.removed_members = removed
            if hasattr(store, "take_drained"):
                for envelope in store.take_drained():
                    self.apply_envelope(space.info.space_id, envelope)
            code = getattr(exc, "code", "invalid-argument")
            details = dict(getattr(exc, "details", {}) or {})
            details.setdefault("message", str(exc))
            raise OrgFsError(str(code), details) from exc
        if hasattr(store, "take_drained"):
            for envelope in store.take_drained():
                self.apply_envelope(space.info.space_id, envelope)
        if self.mesh is not None and hasattr(self.mesh, "broadcast_pending"):
            self.mesh.broadcast_pending(space.info.space_id)

    def _ensure_writable(self, space: _Space) -> None:
        member = space.members.get(self.author)
        if (
            member is None
            or member.mode != "rw"
            or self.author in space.removed_members
        ):
            raise OrgFsError("not-a-member", {"spaceId": space.info.space_id})

    def _space(self, space_id: str) -> _Space:
        try:
            return self._spaces[space_id]
        except KeyError as exc:
            raise OrgFsError("unknown-space", {"spaceId": space_id}) from exc

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
        if path.startswith("id:"):
            node_id = path[3:]
            if node_id not in space.nodes or (
                space.nodes[node_id].deleted and not include_deleted
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
                    and (include_deleted or not node.deleted)
                )
            candidates = sorted(set(next_ids))
            if not candidates:
                raise OrgFsError("unknown-doc", {"node": path})
        return candidates

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
        if node.kind == "doc" and content is not None:
            size = len(content.value().encode())
        elif node.kind == "blob" and node.blob_hash and self.blobs is not None:
            try:
                size = len(self.blobs.get(space.info.space_id, node.blob_hash))
            except Exception:
                size = None
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
        )

    def _snapshot(self, space: _Space) -> dict[str, _NodeSnapshot]:
        return {
            node_id: _NodeSnapshot(
                node_id=node.node_id,
                parent=node.parent,
                name=node.name,
                kind=node.kind,
                doc_id=node.doc_id,
                blob_hash=node.blob_hash,
                deleted=node.deleted,
                version=node.version,
                modified_by=node.modified_by,
                modified_via=node.modified_via,
                content=(
                    space.contents[node.doc_id].value().encode()
                    if node.doc_id in space.contents
                    else b""
                ),
            )
            for node_id, node in space.nodes.items()
        }

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
        self._clock += 1
        space.revision += 1
        version = _version(space.revision, self._clock)
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
            self._events.setdefault(space.info.space_id, []).append(event)
            for watcher in tuple(space.watches):
                if watcher.closed or not fnmatch.fnmatch(info.path, watcher.glob):
                    continue
                watcher.events.append(event)
                watcher.callback(event)

    def _parent_for_new(self, space: _Space, path: str) -> tuple[_Node, str]:
        if path.startswith("id:"):
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
        node_id = uuid.uuid4().hex
        doc_id = f"doc-{uuid.uuid4()}" if kind == "doc" else None
        node = _Node(
            node_id, parent.node_id, name, kind, doc_id=doc_id, modified_by=self.author
        )
        space.nodes[node_id] = node
        if doc_id:
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
        move: tuple[_Node, str] | None = None,
        broadcast: bool = True,
    ) -> None:
        def apply() -> None:
            operation()
            if move is not None:
                node, name = move
                self._clock += 1
                space.tree.record_move(
                    node,
                    timestamp=self._clock,
                    peer=self.node_id,
                    new_parent=node.parent,
                    name=name,
                )
            else:
                for node_id in affected:
                    space.tree.record(space.nodes[node_id])

        self._commit(space, space.tree_doc_id, apply, broadcast=broadcast)
        self._finish(space, affected, changed, old_paths=old_paths)

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
    def invite(self, space_id: str, user: str, mode: MemberMode = "rw") -> MemberInfo:
        space = self._space(space_id)
        if self.author != space.info.owner:
            raise OrgFsError("not-owner")
        if mode not in ("ro", "rw") or not isinstance(user, str) or not user:
            raise OrgFsError("invalid-argument")
        member = MemberInfo(space_id, user, mode, self.author, _now())

        def operation() -> None:
            space.members[user] = member
            space.removed_members.discard(user)
            members = space.meta.get("members", type=Map)
            members[user] = Map(
                {
                    "mode": mode,
                    "addedBy": self.author,
                    "addedAt": member.added_at,
                    "removedAt": None,
                }
            )

        self._commit(space, "meta", operation)
        return member

    @_facade_locked
    def remove_member(self, space_id: str, user: str) -> None:
        space = self._space(space_id)
        if self.author != space.info.owner:
            raise OrgFsError("not-owner")
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
        if space_id in self._spaces:
            return self._spaces[space_id].info
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

        if space_id in self._spaces:
            return self._spaces[space_id].info
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
                node_id,
                raw.get("parent"),
                str(raw.get("name", "")),
                raw.get("kind", "doc"),
                doc_id=raw.get("doc_id"),
                blob_hash=raw.get("blob_hash"),
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
        self._spaces[space_id] = loaded
        return info

    @_facade_locked
    def apply_envelope(self, space_id: str, envelope: bytes) -> None:
        """Refresh an open facade after the durable store admits an envelope."""

        if space_id not in self._spaces:
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

        space = self._spaces[space_id]
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
                            node_id,
                            raw.get("parent"),
                            str(raw.get("name", "")),
                            raw.get("kind", "doc"),
                            raw.get("doc_id"),
                            raw.get("blob_hash"),
                            bool(raw.get("deleted", False)),
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
        active_tree = store.active_tree_doc_id() if store is not None else None
        if old_doc_id == space.tree_doc_id or active_tree == new_doc_id:
            space.tree_doc_id = new_doc_id
            space.tree = TreeDocument(update=snapshot_bytes)
            previous = space.nodes
            rebuilt: dict[str, _Node] = {}
            for node_id, raw in space.tree.materialize().items():
                old = previous.get(node_id)
                rebuilt[node_id] = _Node(
                    node_id,
                    raw.get("parent"),
                    str(raw.get("name", "")),
                    raw.get("kind", "doc"),
                    raw.get("doc_id"),
                    raw.get("blob_hash"),
                    bool(raw.get("deleted", False)),
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
            ),
            key=lambda child: (child.name, child.node_id),
        )
        return tuple(self._node_info(space, child.node_id) for child in children)

    @_facade_locked
    def read_text(self, space_id: str, node: NodeRef) -> tuple[str, str]:
        space = self._space(space_id)
        item = self._one(space, node)
        if item.kind != "doc" or item.doc_id not in space.contents:
            raise OrgFsError("invalid-argument", {"message": "not a text document"})
        return space.contents[item.doc_id].value(), item.version

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

            def initialize() -> None:
                space.contents[created.doc_id or ""].set(content)

            try:
                self._commit(space, created.doc_id or "", initialize)
            except Exception:
                space.nodes.pop(created.node_id, None)
                space.contents.pop(created.doc_id or "", None)
                raise
            self._write_tree(space, lambda: None, [created.node_id], "created")
            return self._node_info(space, created.node_id)
        if existing.kind != "doc":
            raise OrgFsError(
                "invalid-argument", {"message": "target is not a text document"}
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
            return self._node_info(space, existing.node_id)
        old_path = self._path(space, existing.node_id)

        def operation() -> None:
            current_doc.set(value)
            existing.content_frontier += 1

        self._commit(space, existing.doc_id or "", operation)
        self._finish(
            space, [existing.node_id], "content", old_paths={existing.node_id: old_path}
        )
        return self._node_info(space, existing.node_id)

    @_facade_locked
    def read_bytes(self, space_id: str, node: NodeRef) -> bytes:
        space = self._space(space_id)
        item = self._one(space, node)
        if item.kind == "doc":
            return space.contents[item.doc_id or ""].value().encode()
        if item.kind != "blob" or not item.blob_hash:
            raise OrgFsError("invalid-argument", {"message": "not a file"})
        if self.blobs is None:
            raise OrgFsError("blob-unavailable", {"digest": item.blob_hash})
        try:
            return self.blobs.get(space_id, item.blob_hash)
        except Exception as exc:
            if getattr(exc, "code", None) == "purged":
                raise OrgFsError(
                    "purged", dict(getattr(exc, "details", {}) or {})
                ) from exc
            if self.mesh is not None and hasattr(self.mesh, "fetch_blob"):
                try:
                    return self.mesh.fetch_blob(space_id, item.blob_hash)
                except OrgFsError:
                    raise
                except Exception as fetch_exc:
                    raise OrgFsError(
                        "blob-unavailable", {"digest": item.blob_hash}
                    ) from fetch_exc
            raise OrgFsError("blob-unavailable", {"digest": item.blob_hash}) from exc

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

    def export_to(self, space_id: str, node: NodeRef, destination: Path) -> NodeInfo:
        info = self.stat(space_id, node)
        destination.write_bytes(self.read_bytes(space_id, node))
        return info

    @_facade_locked
    def import_from(self, space_id: str, node: NodeRef, source: Path) -> NodeInfo:
        return self.write_bytes(space_id, node, source.read_bytes())

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

        def operation() -> None:
            node.parent = parent.node_id
            node.name = name

        self._write_tree(
            space,
            operation,
            [node.node_id],
            "moved",
            old_paths={node.node_id: old_path},
            move=(node, name),
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
    def history(
        self, space_id: str, node: NodeRef, limit: int = 50, before: str | None = None
    ) -> tuple[HistoryEntry, ...]:
        space = self._space(space_id)
        item = self._one(space, node, include_deleted=True)
        entries = list(reversed(space.history.get(item.node_id, [])))
        if before is not None:
            cutoff = _version_number(before)
            entries = [
                entry
                for entry in entries
                if _version_number(entry.node.version) < cutoff
            ]
        return tuple(
            HistoryEntry(
                self._snapshot_info(
                    space, entry.node, space.snapshots.get(entry.node.version)
                ),
                entry.changed,
                entry.author,
                entry.actor,
                entry.at,
            )
            for entry in entries[: max(0, limit)]
        )

    def _snapshot_info(
        self,
        space: _Space,
        snapshot: _NodeSnapshot,
        all_nodes: Mapping[str, _NodeSnapshot] | None,
    ) -> NodeInfo:
        nodes = {
            node_id: _Node(
                node_id=value.node_id,
                parent=value.parent,
                name=value.name,
                kind=value.kind,
                doc_id=value.doc_id,
                blob_hash=value.blob_hash,
                deleted=value.deleted,
                version=value.version,
                modified_by=value.modified_by,
                modified_via=value.modified_via,
            )
            for node_id, value in (all_nodes or {snapshot.node_id: snapshot}).items()
        }
        return NodeInfo(
            space.info.space_id,
            snapshot.node_id,
            snapshot.kind,
            snapshot.name,
            self._path(space, snapshot.node_id, nodes),
            len(snapshot.content) if snapshot.kind == "doc" else None,
            snapshot.blob_hash,
            snapshot.doc_id,
            snapshot.version,
            snapshot.modified_by,
            snapshot.modified_via,
            self._name_conflict(space, nodes[snapshot.node_id], nodes),
            snapshot.deleted,
        )

    @_facade_locked
    def read_at(self, space_id: str, node: NodeRef, version: str) -> bytes:
        space = self._space(space_id)
        item = self._one(space, node, include_deleted=True)
        snapshot = space.snapshots.get(version, {}).get(item.node_id)
        if snapshot is None:
            raise OrgFsError("unknown-doc", {"version": version})
        if snapshot.kind == "doc":
            return snapshot.content
        if snapshot.kind == "blob" and snapshot.blob_hash:
            if self.blobs is None:
                raise OrgFsError("blob-unavailable", {"digest": snapshot.blob_hash})
            try:
                return self.blobs.get(space_id, snapshot.blob_hash)
            except Exception as exc:
                raise OrgFsError(
                    "blob-unavailable", {"digest": snapshot.blob_hash}
                ) from exc
        raise OrgFsError("invalid-argument", {"message": "node has no bytes"})

    @_facade_locked
    def stat_at(self, space_id: str, node: NodeRef, version: str) -> NodeInfo:
        space = self._space(space_id)
        item = self._one(space, node, include_deleted=True)
        snapshots = space.snapshots.get(version)
        if snapshots is None or item.node_id not in snapshots:
            raise OrgFsError("unknown-doc", {"version": version})
        return self._snapshot_info(space, snapshots[item.node_id], snapshots)

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
                or snap.doc_id not in space.contents
            ):
                continue
            restored_text = snap.content.decode(errors="replace")
            document = space.contents[snap.doc_id]
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

        tree_changed = any(
            snapshots.get(affected_id) is not None
            and (
                space.nodes[affected_id].parent,
                space.nodes[affected_id].name,
                space.nodes[affected_id].kind,
                space.nodes[affected_id].doc_id,
                space.nodes[affected_id].blob_hash,
                space.nodes[affected_id].deleted,
            )
            != (
                snapshots[affected_id].parent,
                snapshots[affected_id].name,
                snapshots[affected_id].kind,
                snapshots[affected_id].doc_id,
                snapshots[affected_id].blob_hash,
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
                restored.deleted = False
                space.tree.record(restored)

        if tree_changed:
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

    @_facade_locked
    def watch(
        self, space_id: str, glob: str, callback: Callable[[ChangeEvent], None]
    ) -> _Watch:
        watcher = _Watch(glob, callback)
        self._space(space_id).watches.append(watcher)
        return watcher

    @_facade_locked
    def changes_since(
        self, space_id: str, glob: str = "*", since_version: str | None = None
    ) -> tuple[ChangeEvent, ...]:
        """Return the polling watch projection in commit order."""

        self._space(space_id)
        cutoff = _version_number(since_version) if since_version is not None else -1
        return tuple(
            event
            for event in self._events.get(space_id, ())
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

        def commit(mutate: Callable[[Doc], None]) -> NodeInfo:
            with self._lock:
                current_space = self._space(space_id)
                current = self._one(current_space, f"id:{node_id}")
                if current.doc_id != doc_id or doc_id not in current_space.contents:
                    raise OrgFsError(
                        "snapshot-barrier",
                        {"retiredDocId": doc_id, "replacementDocId": current.doc_id},
                    )
                self._ensure_writable(current_space)
                live = current_space.contents[doc_id]
                working = _ContentDocument(
                    client_id=live.doc.client_id, update=live.export()
                )
                before = live.doc.get_state()
                mutate(working.doc)
                update = working.doc.get_update(before)
                store = self._store(space_id)
                if store is not None:
                    try:
                        store.commit(
                            doc_id,
                            lambda doc: doc.apply_update(update),
                            author=self.author,
                            actor=self.actor,
                        )
                    except Exception as exc:
                        code = getattr(exc, "code", "invalid-argument")
                        details = dict(getattr(exc, "details", {}) or {})
                        details.setdefault("message", str(exc))
                        raise OrgFsError(str(code), details) from exc
                live.doc.apply_update(update)
                current.content_frontier += 1
                if store is not None and hasattr(store, "take_drained"):
                    for envelope in store.take_drained():
                        self.apply_envelope(space_id, envelope)
                if self.mesh is not None and hasattr(self.mesh, "broadcast_pending"):
                    self.mesh.broadcast_pending(space_id)
                self._finish(current_space, [node_id], "content")
                return self._node_info(current_space, node_id)

        def version() -> str:
            with self._lock:
                current_space = self._space(space_id)
                return self._node_info(current_space, node_id).version

        return StructuredOrgDoc(document, doc_id, commit, version=version)

    def _require_purge_owner(self, space: _Space) -> None:
        if self.author != space.info.owner:
            raise OrgFsError("not-owner", {"spaceId": space.info.space_id})

    def _export_behind_writer_work(
        self, space: _Space, retired_doc_id: str, plan_id: str
    ) -> NodeInfo | None:
        """Export unpublished retired-doc text as a normal root file for review."""

        store = self._store(space.info.space_id)
        if (
            store is None
            or not hasattr(store, "has_unbroadcast_local_doc")
            or not store.has_unbroadcast_local_doc(retired_doc_id)
            or retired_doc_id not in space.contents
        ):
            return None
        content = space.contents[retired_doc_id].value()
        name = f"purge-conflict-{plan_id[:12] or 'unknown'}.md"
        try:
            existing = self.resolve(space.info.space_id, name)
        except OrgFsError as exc:
            if exc.code != "unknown-doc":
                raise
            existing = ()
        if existing:
            return existing[0]
        return self.write_text(space.info.space_id, name, content)

    def _normalize_purge_targets(
        self, space: _Space, targets: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[str, str], ...]:
        normalized: list[dict[str, str]] = []
        for target in targets:
            kind = target.get("kind") if isinstance(target, Mapping) else None
            if kind == "blob":
                sha = target.get("sha")
                if not isinstance(sha, str) or len(sha) != 64:
                    raise OrgFsError(
                        "invalid-argument", {"message": "invalid blob sha"}
                    )
                normalized.append({"kind": "blob", "sha": sha})
            elif kind == "tree-range":
                start = target.get("from")
                end = target.get("to")
                if not isinstance(start, str) or not isinstance(end, str):
                    raise OrgFsError(
                        "invalid-argument", {"message": "tree range requires from/to"}
                    )
                normalized.append(
                    {
                        "kind": "tree-range",
                        "docId": space.tree_doc_id,
                        "from": start,
                        "to": end,
                    }
                )
            elif kind == "doc-range":
                node_ref = target.get("node")
                start = target.get("from")
                end = target.get("to")
                if not all(isinstance(value, str) for value in (node_ref, start, end)):
                    raise OrgFsError(
                        "invalid-argument",
                        {"message": "doc range requires node/from/to"},
                    )
                node = self._one(space, str(node_ref), include_deleted=True)
                if node.kind != "doc" or not node.doc_id:
                    raise OrgFsError(
                        "invalid-argument", {"message": "target is not a document"}
                    )
                normalized.append(
                    {
                        "kind": "doc-range",
                        "docId": node.doc_id,
                        "nodeId": node.node_id,
                        "from": str(start),
                        "to": str(end),
                    }
                )
            else:
                raise OrgFsError(
                    "invalid-argument", {"message": "unknown purge target"}
                )
        if not normalized:
            raise OrgFsError("invalid-argument", {"message": "purge targets are empty"})
        return tuple(normalized)

    def _build_purge_plan(
        self,
        space: _Space,
        targets: tuple[dict[str, str], ...],
        *,
        expires_at: str | None = None,
    ) -> PurgePlan:
        store = self._store(space.info.space_id)
        if store is None or not hasattr(store, "purge_inventory"):
            raise OrgFsError(
                "invalid-argument", {"message": "purge requires a durable store"}
            )
        documents: list[PurgeDocument] = []
        snapshots: list[PurgeSnapshot] = []
        writers: set[str] = set()
        authors: dict[str, int] = {}
        blob_hashes = {target["sha"] for target in targets if target["kind"] == "blob"}
        for target in targets:
            if target["kind"] == "blob":
                continue
            inventory = store.purge_inventory(target["docId"])
            documents.append(
                PurgeDocument(
                    target["docId"],
                    target["from"],
                    target["to"],
                    tuple(inventory["updateBlobs"]),
                    tuple(inventory["logKeys"]),
                )
            )
            blob_hashes.update(inventory["updateBlobs"])
            snapshots.extend(
                PurgeSnapshot(item["snapshotId"], item["docId"])
                for item in inventory["snapshots"]
            )
            writers.update(inventory["writersAffected"])
            for author, count in inventory["authorsBefore"].items():
                authors[author] = authors.get(author, 0) + int(count)
        blobs: list[PurgeBlob] = []
        for sha in sorted(blob_hashes):
            elsewhere: tuple[str, ...] = ()
            if self.blobs is not None and hasattr(self.blobs, "referenced_elsewhere"):
                elsewhere = tuple(
                    self.blobs.referenced_elsewhere(space.info.space_id, sha)
                )
            blobs.append(PurgeBlob(sha, elsewhere))
        return PurgePlan.create(
            space_id=space.info.space_id,
            meta_frontier=base64.b64encode(store.frontier("meta")).decode("ascii"),
            docs=sorted(documents, key=lambda item: item.doc_id),
            blobs=blobs,
            snapshots=sorted(
                snapshots, key=lambda item: (item.doc_id, item.snapshot_id)
            ),
            writers_affected=sorted(writers),
            authors_before=authors,
            expires_at=expires_at,
        )

    @_facade_locked
    def purge_plan(
        self, space_id: str, targets: Sequence[Mapping[str, Any]]
    ) -> PurgePlan:
        space = self._space(space_id)
        self._require_purge_owner(space)
        normalized = self._normalize_purge_targets(space, targets)
        plan = self._build_purge_plan(space, normalized)
        self._purge_plans[plan.plan_id] = (plan, normalized)
        store = self._store(space_id)
        if store is not None and hasattr(store, "save_purge_plan"):
            store.save_purge_plan(plan.plan_id, plan.storage_dict(), normalized)
        return plan

    def _purge_participants(self, space: _Space) -> tuple[str, ...]:
        roots = space.meta.get("writers", type=Map)
        raw_writers = roots.to_py() if roots is not None else {}
        nodes_by_user: dict[str, set[str]] = {}
        for value in raw_writers.values():
            if not isinstance(value, dict) or not value.get("node"):
                continue
            author = value.get("author")
            if isinstance(author, str):
                nodes_by_user.setdefault(author, set()).add(str(value["node"]))
        if self.author in space.members:
            nodes_by_user.setdefault(self.author, set()).add(self.node_id)
        participants: set[str] = set()
        for user in space.members:
            bound = nodes_by_user.get(user)
            # A user URI in ``pending`` is the no-node-binding reason marker.
            participants.update(bound if bound else (user,))
        return tuple(sorted(participants))

    @_facade_locked
    def purge(self, space_id: str, plan_id: str) -> PurgeResult:
        space = self._space(space_id)
        self._require_purge_owner(space)
        store = self._store(space_id)
        saved = self._purge_plans.get(plan_id)
        if saved is None and store is not None and hasattr(store, "load_purge_plan"):
            loaded = store.load_purge_plan(plan_id)
            if loaded is not None:
                try:
                    plan = PurgePlan.from_storage_dict(loaded[0])
                except (KeyError, TypeError, ValueError) as exc:
                    raise OrgFsError("stale-plan", {"planId": plan_id}) from exc
                saved = (plan, loaded[1])
                self._purge_plans[plan_id] = saved
        if saved is None:
            raise OrgFsError("stale-plan", {"planId": plan_id})
        plan, targets = saved
        if utc_now() >= parse_iso(plan.expires_at):
            raise OrgFsError("stale-plan", {"planId": plan_id, "reason": "expired"})
        try:
            current = self._build_purge_plan(space, targets, expires_at=plan.expires_at)
        except Exception as exc:
            if isinstance(exc, OrgFsError):
                raise
            raise OrgFsError("stale-plan", {"planId": plan_id}) from exc
        if current.plan_id != plan.plan_id:
            raise OrgFsError("stale-plan", {"planId": plan_id})

        replacements: dict[str, tuple[str, bytes, str]] = {}
        content_nodes: dict[str, str] = {}
        replacement_ids: dict[str, str] = {}
        for target in targets:
            if target["kind"] == "blob":
                continue
            old_doc_id = target["docId"]
            if target["kind"] == "tree-range":
                replacement_ids[old_doc_id] = f"tree-{uuid.uuid4()}"
            else:
                new_doc_id = f"doc-{uuid.uuid4()}"
                replacement_ids[old_doc_id] = new_doc_id
                content_nodes[target["nodeId"]] = new_doc_id
        for target in targets:
            if target["kind"] == "blob":
                continue
            old_doc_id = target["docId"]
            new_doc_id = replacement_ids[old_doc_id]
            if target["kind"] == "tree-range":
                rebuilt = TreeDocument(client_id=space.tree.doc.client_id)
                for node in space.nodes.values():
                    if node.deleted:
                        continue
                    rebuilt.record(
                        replace(
                            node, doc_id=content_nodes.get(node.node_id, node.doc_id)
                        )
                    )
                snapshot_bytes = rebuilt.get_update()
            else:
                rebuilt_content = _ContentDocument(
                    client_id=space.contents[old_doc_id].doc.client_id
                )
                rebuilt_content.set(space.contents[old_doc_id].value())
                snapshot_bytes = rebuilt_content.export()
            replacements[old_doc_id] = (
                new_doc_id,
                snapshot_bytes,
                hashlib.sha256(snapshot_bytes).hexdigest(),
            )

        retired_at = _now()

        def commit_retirements() -> None:
            plans = space.meta.get("purgePlans", type=Map)
            plans[plan_id] = Map(
                {"expiresAt": plan.expires_at, "createdAt": retired_at}
            )
            retirements = space.meta.get("retirements", type=Map)
            snapshot_points = space.meta.get("snapshotPoints", type=Map)
            docs = space.meta.get("docs", type=Map)
            for old_doc_id, (
                new_doc_id,
                _snapshot,
                snapshot_id,
            ) in replacements.items():
                retirements[old_doc_id] = Map(
                    {
                        "replacementDocId": new_doc_id,
                        "planId": plan_id,
                        "snapshotId": snapshot_id,
                        "retiredAt": retired_at,
                    }
                )
                snapshot_points[new_doc_id] = Map(
                    {
                        "frontier": base64.b64encode(
                            encode_snapshot_frontier({})
                        ).decode("ascii"),
                        "snapshotId": snapshot_id,
                        "planId": plan_id,
                    }
                )
                if old_doc_id == space.tree_doc_id:
                    docs["tree"] = Map(
                        {
                            "active": new_doc_id,
                            "activatedAt": retired_at,
                            "planId": plan_id,
                        }
                    )
            purge_list = space.meta.get("purgeList", type=Map)
            for blob in plan.blobs:
                purge_list[blob.sha] = Map(
                    {"planId": plan_id, "bannedAt": retired_at, "unbannedAt": None}
                )
            audits = space.meta.get("purgeAudit", type=Map)
            audits[plan_id] = Map(
                {
                    "at": retired_at,
                    "nodeId": self.node_id,
                    "docIds": json.dumps(sorted(replacements)),
                    "blobHashes": json.dumps(sorted(blob.sha for blob in plan.blobs)),
                }
            )

        # G2 step 3: make the retirement durable locally, but do not publish it
        # until every replacement snapshot is queryable by a remote member.
        self._commit(space, "meta", commit_retirements, broadcast=False)
        if hasattr(store, "_fault"):
            store._fault("retirement_committed")

        old_tree_id = space.tree_doc_id
        for old_doc_id, (
            new_doc_id,
            snapshot_bytes,
            _snapshot_id,
        ) in replacements.items():
            store.install_replacement(
                old_doc_id,
                new_doc_id,
                snapshot_bytes,
                author=self.author,
                actor=self.actor,
            )
            if old_doc_id == old_tree_id:
                space.tree_doc_id = new_doc_id
                space.tree = TreeDocument(update=snapshot_bytes)
                space.nodes = {
                    node_id: node
                    for node_id, node in space.nodes.items()
                    if not node.deleted
                }
            else:
                space.contents[new_doc_id] = _ContentDocument(update=snapshot_bytes)
                space.contents.pop(old_doc_id, None)
        if content_nodes and old_tree_id not in replacements:
            affected = tuple(content_nodes)

            def rewrite_pointers() -> None:
                for node_id, new_doc_id in content_nodes.items():
                    space.nodes[node_id].doc_id = new_doc_id

            self._write_tree(
                space,
                rewrite_pointers,
                affected,
                "content",
                broadcast=False,
            )
        else:
            for node_id, new_doc_id in content_nodes.items():
                space.nodes[node_id].doc_id = new_doc_id

        def publish_snapshot_frontiers() -> None:
            snapshot_points = space.meta.get("snapshotPoints", type=Map)
            for _old_doc_id, (
                new_doc_id,
                _snapshot_bytes,
                snapshot_id,
            ) in replacements.items():
                watermarks = (
                    store.writer_seq_watermarks(new_doc_id)
                    if store is not None and hasattr(store, "writer_seq_watermarks")
                    else {}
                )
                snapshot_points[new_doc_id] = Map(
                    {
                        "frontier": base64.b64encode(
                            encode_snapshot_frontier(watermarks)
                        ).decode("ascii"),
                        "snapshotId": snapshot_id,
                        "planId": plan_id,
                    }
                )

        self._commit(space, "meta", publish_snapshot_frontiers, broadcast=False)
        if self.mesh is not None and hasattr(self.mesh, "broadcast_pending"):
            self.mesh.broadcast_pending(space.info.space_id)

        for old_doc_id in replacements:
            store.delete_retired_objects(old_doc_id)
            if store.retired_residue(old_doc_id):
                raise OrgFsError(
                    "invalid-argument",
                    {
                        "message": "retired document residue remains",
                        "docId": old_doc_id,
                    },
                )
        if self.blobs is not None:
            for blob in plan.blobs:
                if hasattr(self.blobs, "release"):
                    self.blobs.release(space_id, blob.sha)
                if hasattr(self.blobs, "delete_if_unreferenced"):
                    self.blobs.delete_if_unreferenced(blob.sha)

        if hasattr(store, "_fault"):
            store._fault("before_purge_ack")

        def acknowledge() -> None:
            acks = space.meta.get("purgeAcks", type=Map)
            current = (acks.to_py() or {}).get(self.node_id)
            values = {
                key: Map(value)
                for key, value in (current.items() if isinstance(current, dict) else ())
                if isinstance(value, dict)
            }
            values[plan_id] = Map({"ackedAt": _now()})
            acks[self.node_id] = Map(values)

        self._commit(space, "meta", acknowledge)
        status = self.purge_status(space_id, plan_id)
        return PurgeResult(plan_id, True, status.acknowledged, status.pending)

    @_facade_locked
    def purge_status(self, space_id: str, plan_id: str) -> PurgeStatus:
        space = self._space(space_id)
        plans = space.meta.get("purgePlans", type=Map)
        durable_plans = plans.to_py() if plans is not None else {}
        if plan_id not in self._purge_plans and plan_id not in durable_plans:
            store = self._store(space_id)
            loaded = (
                store.load_purge_plan(plan_id)
                if store is not None and hasattr(store, "load_purge_plan")
                else None
            )
            if loaded is None:
                raise OrgFsError("stale-plan", {"planId": plan_id})
            try:
                plan = PurgePlan.from_storage_dict(loaded[0])
            except (KeyError, TypeError, ValueError) as exc:
                raise OrgFsError("stale-plan", {"planId": plan_id}) from exc
            if plan.plan_id != plan_id:
                raise OrgFsError("stale-plan", {"planId": plan_id})
            self._purge_plans[plan_id] = (plan, loaded[1])
        acks = space.meta.get("purgeAcks", type=Map)
        raw = acks.to_py() if acks is not None else {}
        acknowledged = tuple(
            sorted(
                node
                for node, plans in raw.items()
                if isinstance(plans, dict) and plan_id in plans
            )
        )
        pending = tuple(
            node for node in self._purge_participants(space) if node not in acknowledged
        )
        return PurgeStatus(plan_id, acknowledged, pending)

    @_facade_locked
    def unban(self, space_id: str, sha: str) -> None:
        space = self._space(space_id)
        self._require_purge_owner(space)
        if len(sha) != 64 or any(
            character not in "0123456789abcdef" for character in sha
        ):
            raise OrgFsError("invalid-argument", {"sha": sha})

        def operation() -> None:
            purge_list = space.meta.get("purgeList", type=Map)
            current = (purge_list.to_py() or {}).get(sha)
            if not isinstance(current, dict):
                raise OrgFsError("invalid-argument", {"sha": sha})
            purge_list[sha] = Map({**current, "unbannedAt": _now()})

        self._commit(space, "meta", operation)

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
        return SpaceStatus(
            space_id,
            unconfirmed,
            tuple(node for node, durable in holders if durable),
            tuple(node for node, _durable in holders),
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
                    node_id,
                    raw.get("parent"),
                    raw.get("name", ""),
                    raw.get("kind", "doc"),
                    raw.get("doc_id"),
                    raw.get("blob_hash"),
                    raw.get("deleted", False),
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
                node.deleted = raw.get("deleted", node.deleted)

    def export_content_update(self, space_id: str, node: NodeRef) -> bytes:
        space = self._space(space_id)
        item = self._one(space, node)
        if item.kind != "doc":
            raise OrgFsError("invalid-argument")
        return space.contents[item.doc_id or ""].export()

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


def _raise_invalid(message: str) -> Any:
    raise OrgFsError("invalid-argument", {"message": message})
