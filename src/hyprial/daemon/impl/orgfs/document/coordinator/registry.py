# Decision record (phase D, package D1, 2026-11-25):
# - Design §2.5: one authoritative ``Doc`` per document, held here per space.
#   meta and tree stay resident; content documents load lazily into an LRU of
#   256 entries.  A document used by the running space command is pinned and
#   never evicted; pins exist only inside a command (reads holding the space
#   lock rely on recency), so a read-only workload cannot pin the whole space.
# - Design §2.5: one random 53-bit client id per space per process; every
#   authoritative ``Doc`` of the space uses it.  Reopening a broken store keeps
#   the id: the reloaded documents equal disk, so the id continues from there.
# - Design §3.6 "export swaps the instance": every export of an authoritative
#   document (to rebuild a staged copy, to stage a content update, to compact)
#   replaces the instance by one loaded from the same export, in place for the
#   tree and content wrappers so callers holding them stay valid.
# - Design §9 R12: ``vectors`` is an immutable mapping replaced after every
#   commit or accepted update, read without the space lock.
# - Design §9 R4: ``unverified`` and ``pending`` are ordered sets of content
#   doc ids; an unverified document counts as pending for ``status``.
# - Design §9 R1: the base-text cache keeps at most 256 entries and 16 MiB.
# - Design §9 R10: the state tree materializes with ``membership.materialize_tree``
#   and never writes a root row of its own (§3.3, R7).
# - F1 (T3 report §4-1, design §3.6 "合并后无环：把后到的移动判负"): a local
#   move rewrites the row's ``parent`` as well as appending a move record, so
#   concurrent A->B and B->A merge into rows whose parents form a cycle that
#   move replay cannot undo (both replays are themselves cycle-making).
#   ``break_cycles`` then settles every remaining cycle the same way on every
#   replica: the cycle member whose latest move sorts last by
#   ``(ts, peer, node)`` loses and goes back to that move's ``oldParent``, or
#   to the root when that would still cycle or is unknown; a member with no
#   move record sorts before every moved one, ties fall to the node id.  The
#   inputs (row map, move array) are CRDT-converged, so the result is too.
# - F2 (F1 report §7): ``break_cycles`` and ``_move_order`` moved verbatim into
#   ``membership/staged.py``; ``materialize_tree`` now ends with the cycle
#   break, so admission and the facade place a merged cycle identically.  The
#   name stays importable here for ``coordinator/tree/test_break_cycles.py``.
# Decision record (phase D, package F3, 2026-11-25):
# - A write must not cost in proportion to the space (V report §5, F3
#   profile).  ``vectors`` is a ``VectorTable``: still an immutable snapshot
#   for the lock-free ``read_vectors``, but persistent (three 32-way hash
#   levels over small leaf dicts), so publishing a commit's vectors copies a
#   few small nodes instead of the whole table.
# - ``doc_index`` is a ``NodeIndex`` over one ``space.nodes`` dict: children by
#   ``(parent, name)`` and nodes by doc id, built once and then kept by
#   ``note(node)`` from the places that change a node locally (row record, new
#   node).  Every other change (remote tree, rematerialize, reload, reset,
#   permanent removal) still drops it for a rebuild.  Lookups re-check each
#   candidate's current fields, so a stale entry is skipped; an index bound to
#   another ``space.nodes`` dict is rebuilt by its user.
# - Among several nodes sharing a doc id the index keeps the rebuild's choice
#   (the last live one, else the first) in registration order, which is the
#   order of ``space.nodes`` for every known path.
# - F3 follow-up: ``doc_nodes`` answers "every node of this doc id" for the
#   remote content projection, which scanned the whole table per update.
"""Per-space document registry of the state host (design §2.5)."""

from __future__ import annotations

from collections import OrderedDict
import json
import secrets
from typing import Any, Callable, Mapping

from pycrdt import Array, Doc, Map, Text

from hyprial.daemon.impl.orgfs.document.model import TreeDocument, _ContentDocument
from hyprial.daemon.impl.orgfs.membership import (
    CommitStamp,
    RejectionMemory,
    StagedDoc,
    break_cycles as break_cycles,
    materialize_tree,
)
from hyprial.daemon.impl.orgfs.state import StateStore

CONTENT_CAPACITY = 256
BASE_TEXT_ENTRIES = 256
BASE_TEXT_BYTES = 16 * 1024 * 1024
PUBLISH_QUEUE_LIMIT = 256
EMPTY_VECTOR = b"\x00"


def new_client_id() -> int:
    """A random non-zero 53-bit Yjs client id."""

    return secrets.randbits(53) or 1


class SealedContents(dict):
    """The legacy ``_Space.contents`` slot; the state host must never use it."""

    def _sealed(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("space.contents is not used by the state host")

    __getitem__ = __setitem__ = __delitem__ = __contains__ = __iter__ = __len__ = _sealed
    get = pop = setdefault = update = items = keys = values = copy = _sealed


def parse_row(raw: object) -> dict[str, Any] | None:
    """Parse one stored tree row; ``None`` when it is not a JSON object."""

    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


class StateTree(TreeDocument):
    """The tree document of the state host: no implicit root row."""

    def __init__(self, *, client_id: int, update: bytes | None = None) -> None:
        doc = Doc(client_id=client_id)
        if update:
            doc.apply_update(update)
        self.rebind(doc)

    def rebind(self, doc: Doc) -> None:
        """Install ``doc`` as the authoritative tree instance."""

        self.doc = doc
        self.nodes = doc.get("nodes", type=Map)
        self.moves = doc.get("moves", type=Array)

    def rows(self) -> dict[str, dict[str, Any]]:
        """Every stored row that parses as a JSON object."""

        rows: dict[str, dict[str, Any]] = {}
        for node_id, raw in (self.nodes.to_py() or {}).items():
            value = parse_row(raw)
            if value is not None:
                rows[str(node_id)] = value
        return rows

    def row(self, node_id: str) -> dict[str, Any] | None:
        """The stored row of ``node_id``."""

        return parse_row(self.nodes.get(node_id)) if node_id in self.nodes else None

    def materialize(self) -> dict[str, dict[str, Any]]:
        moves = [
            value
            for value in (parse_row(raw) for raw in (self.moves.to_py() or []))
            if value is not None
        ]
        return materialize_tree(self.rows(), moves)


class BaseTexts:
    """Bounded ``(node_id, version) -> text`` cache for ``base_version`` merges."""

    def __init__(self, entries: int = BASE_TEXT_ENTRIES, size: int = BASE_TEXT_BYTES) -> None:
        self._entries = entries
        self._size = size
        self._bytes = 0
        self._texts: OrderedDict[tuple[str, str], str] = OrderedDict()

    def remember(self, node_id: str, version: str, text: str) -> None:
        key = (node_id, version)
        cost = len(text.encode())
        if cost > self._size:
            return
        previous = self._texts.pop(key, None)
        if previous is not None:
            self._bytes -= len(previous.encode())
        self._texts[key] = text
        self._bytes += cost
        while len(self._texts) > self._entries or self._bytes > self._size:
            _key, dropped = self._texts.popitem(last=False)
            self._bytes -= len(dropped.encode())

    def get(self, node_id: str, version: str) -> str | None:
        key = (node_id, version)
        text = self._texts.get(key)
        if text is not None:
            self._texts.move_to_end(key)
        return text

    def __len__(self) -> int:
        return len(self._texts)


class DocRegistry:
    """Authoritative documents, staged copies and coordinator state of one space."""

    def __init__(
        self,
        space_id: str,
        store: StateStore,
        *,
        client_id: int | None = None,
        capacity: int = CONTENT_CAPACITY,
    ) -> None:
        if capacity < 1:
            raise ValueError("content capacity must be positive")
        self.space_id = space_id
        self.store = store
        self.client_id = client_id or new_client_id()
        self.capacity = capacity
        self.meta = Doc(client_id=self.client_id)
        self.tree = StateTree(client_id=self.client_id)
        self.staged_meta = StagedDoc("meta")
        self.staged_tree = StagedDoc("tree")
        self.contents: OrderedDict[str, _ContentDocument] = OrderedDict()
        self.pinned: set[str] = set()
        self.in_command = 0
        self.vectors: Mapping[str, bytes] = VectorTable()
        self.generation = 0
        self.batches: dict[str, str] = {}
        self.unverified: dict[str, None] = {}
        self.pending: dict[str, None] = {}
        self.rejections = RejectionMemory()
        self.rejected_updates = 0
        self.frontier_repairs = 0
        self.announced_blobs: dict[str, str] = {}
        self.stamps: dict[str, CommitStamp] = {}
        self.base_texts = BaseTexts()
        self.doc_index: NodeIndex | None = None
        self.broken = False
        self.on_meta: Callable[[Doc], None] | None = None

    def load_directory(self) -> None:
        """(Re)load meta and tree from the store and rebuild both staged copies."""

        meta = Doc(client_id=self.client_id)
        meta_state = self.store.load("meta")
        if meta_state:
            meta.apply_update(meta_state)
        tree_state = self.store.load("tree")
        self.tree.rebind(StateTree(client_id=self.client_id, update=tree_state).doc)
        self._install_meta(meta)
        self.staged_meta.reset(meta.get_update())
        self.staged_tree.reset(self.tree.doc.get_update())
        self.contents.clear()
        self.pinned.clear()
        self.doc_index = None
        self.vectors = VectorTable({"meta": meta.get_state(), "tree": self.tree.doc.get_state()})

    def _install_meta(self, doc: Doc) -> None:
        self.meta = doc
        if self.on_meta is not None:
            self.on_meta(doc)

    def begin_command(self) -> None:
        self.in_command += 1

    def end_command(self) -> None:
        self.in_command = max(0, self.in_command - 1)
        if not self.in_command:
            self.pinned.clear()
            self._evict()

    def _pin(self, doc_id: str) -> None:
        if self.in_command:
            self.pinned.add(doc_id)

    def _evict(self) -> None:
        while len(self.contents) > self.capacity:
            victim = next((doc_id for doc_id in self.contents if doc_id not in self.pinned), None)
            if victim is None:
                return
            del self.contents[victim]

    def loaded(self, doc_id: str) -> _ContentDocument | None:
        """The content document when it is in memory; never loads."""

        return self.contents.get(doc_id)

    def content(self, doc_id: str) -> _ContentDocument:
        """The content document, loaded from the store on first use and pinned."""

        document = self.contents.get(doc_id)
        if document is None:
            document = _ContentDocument(client_id=self.client_id, update=self.store.load(doc_id))
            self.contents[doc_id] = document
            self._pin(doc_id)
            self._evict()
        else:
            self.contents.move_to_end(doc_id)
            self._pin(doc_id)
        return document

    def create_content(self, doc_id: str) -> _ContentDocument:
        """A new empty content document (a new file's, not yet committed)."""

        document = _ContentDocument(client_id=self.client_id)
        self.contents[doc_id] = document
        self.contents.move_to_end(doc_id)
        self._pin(doc_id)
        self._evict()
        return document

    def put_content(self, doc_id: str, document: _ContentDocument) -> None:
        self.contents[doc_id] = document
        self.contents.move_to_end(doc_id)
        self._pin(doc_id)
        self._evict()

    def drop(self, doc_id: str) -> None:
        """Forget a content document's instance and its coordinator state."""

        self.contents.pop(doc_id, None)
        self.pinned.discard(doc_id)
        self.unverified.pop(doc_id, None)
        self.pending.pop(doc_id, None)

    def document(self, doc_id: str) -> Doc:
        """The authoritative ``Doc`` of ``doc_id``."""

        if doc_id == "meta":
            return self.meta
        if doc_id == "tree":
            return self.tree.doc
        return self.content(doc_id).doc

    def export_and_swap(self, doc_id: str) -> bytes:
        """Export the authoritative state and replace the instance by its reload."""

        if doc_id == "meta":
            fresh = self.staged_meta.reset_from_authority(self.meta)
            self._install_meta(fresh)
            return fresh.get_update()
        if doc_id == "tree":
            fresh = self.staged_tree.reset_from_authority(self.tree.doc)
            self.tree.rebind(fresh)
            return fresh.get_update()
        document = self.content(doc_id)
        staged = StagedDoc("content")
        swap_content(document, staged.reset_from_authority(document.doc))
        return document.doc.get_update()

    def stage_content(self, doc_id: str) -> StagedDoc:
        """A throwaway staged copy of a content document (export swaps the instance)."""

        document = self.content(doc_id)
        staged = StagedDoc("content")
        swap_content(document, staged.reset_from_authority(document.doc))
        return staged

    def reload(self, doc_ids: tuple[str, ...]) -> None:
        """Replace the instances of ``doc_ids`` by what the store holds."""

        if "meta" in doc_ids or "tree" in doc_ids:
            meta = Doc(client_id=self.client_id)
            meta_state = self.store.load("meta")
            tree_state = self.store.load("tree")
            if meta_state:
                meta.apply_update(meta_state)
            self._install_meta(meta)
            self.tree.rebind(StateTree(client_id=self.client_id, update=tree_state).doc)
            self.staged_meta.reset(meta.get_update())
            self.staged_tree.reset(self.tree.doc.get_update())
            self.doc_index = None
        for doc_id in doc_ids:
            if doc_id in ("meta", "tree"):
                continue
            document = self.contents.get(doc_id)
            if document is None:
                continue
            fresh = Doc(client_id=self.client_id)
            state = self.store.load(doc_id)
            if state:
                fresh.apply_update(state)
            swap_content(document, fresh)
        self.refresh_vectors(doc_ids)

    def refresh_vectors(self, doc_ids: tuple[str, ...] | list[str]) -> None:
        """Publish the current vectors of ``doc_ids`` in a new immutable table."""

        updates: dict[str, bytes] = {}
        for doc_id in doc_ids:
            if doc_id == "meta":
                updates[doc_id] = self.meta.get_state()
            elif doc_id == "tree":
                updates[doc_id] = self.tree.doc.get_state()
            else:
                document = self.contents.get(doc_id)
                if document is not None:
                    updates[doc_id] = document.doc.get_state()
        self.vectors = self._vector_table().updated(updates)

    def remember_vector(self, doc_id: str, vector: bytes) -> None:
        self.vectors = self._vector_table().updated({doc_id: vector})

    def forget_vectors(self, doc_ids: tuple[str, ...] | list[str]) -> None:
        table = self._vector_table()
        if any(doc_id in table for doc_id in doc_ids):
            self.vectors = table.updated({}, removals=doc_ids)

    def _vector_table(self) -> "VectorTable":
        table = self.vectors
        return table if isinstance(table, VectorTable) else VectorTable(table)

    def mark(self, doc_id: str, covered: bool | None) -> None:
        """Record what is known about a content document's coverage."""

        self.unverified.pop(doc_id, None)
        if covered is None:
            self.pending.pop(doc_id, None)
            self.unverified[doc_id] = None
        elif covered:
            self.pending.pop(doc_id, None)
        else:
            self.pending[doc_id] = None

    def reset(self, store: StateStore) -> None:
        """Start over on a reopened store: every instance comes from disk again."""

        self.store = store
        self.broken = False
        self.stamps.clear()
        self.batches.clear()
        self.pending.clear()
        self.unverified.clear()
        self.vectors = VectorTable()
        self.load_directory()


_LEVEL_BITS = 5
_LEVEL_MASK = (1 << _LEVEL_BITS) - 1
_EMPTY_LEVEL: tuple[Any, ...] = (None,) * (1 << _LEVEL_BITS)


class VectorTable(Mapping[str, bytes]):
    """An immutable doc id -> state vector map whose updates share structure."""

    __slots__ = ("_root", "_size")

    def __init__(self, items: Mapping[str, bytes] | None = None) -> None:
        self._root: tuple[Any, ...] = _EMPTY_LEVEL
        self._size = 0
        if items:
            built = VectorTable().updated(dict(items))
            self._root, self._size = built._root, built._size

    @staticmethod
    def _slots(doc_id: str) -> tuple[int, int, int]:
        value = hash(doc_id)
        return (
            value & _LEVEL_MASK,
            (value >> _LEVEL_BITS) & _LEVEL_MASK,
            (value >> (2 * _LEVEL_BITS)) & _LEVEL_MASK,
        )

    def _leaf(self, doc_id: str) -> dict[str, bytes] | None:
        first, second, third = self._slots(doc_id)
        middle = self._root[first]
        if middle is None:
            return None
        bottom = middle[second]
        return None if bottom is None else bottom[third]

    def __getitem__(self, doc_id: str) -> bytes:
        leaf = self._leaf(doc_id)
        if leaf is None:
            raise KeyError(doc_id)
        return leaf[doc_id]

    def __contains__(self, doc_id: object) -> bool:
        if not isinstance(doc_id, str):
            return False
        leaf = self._leaf(doc_id)
        return leaf is not None and doc_id in leaf

    def get(self, doc_id: str, default: Any = None) -> Any:
        leaf = self._leaf(doc_id)
        return default if leaf is None else leaf.get(doc_id, default)

    def __len__(self) -> int:
        return self._size

    def __iter__(self) -> Any:
        for middle in self._root:
            if middle is None:
                continue
            for bottom in middle:
                if bottom is None:
                    continue
                for leaf in bottom:
                    if leaf is not None:
                        yield from leaf

    def updated(
        self, updates: Mapping[str, bytes], *, removals: Any = ()
    ) -> "VectorTable":
        """A new table with ``updates`` set and ``removals`` gone; self is unchanged."""

        changes: dict[str, bytes | None] = {doc_id: None for doc_id in removals}
        changes.update(updates)
        if not changes:
            return self
        root = list(self._root)
        middles: dict[int, list[Any]] = {}
        bottoms: dict[tuple[int, int], list[Any]] = {}
        leaves: dict[tuple[int, int, int], dict[str, bytes]] = {}
        size = self._size
        for doc_id, vector in changes.items():
            first, second, third = self._slots(doc_id)
            if first not in middles:
                middles[first] = list(root[first] or _EMPTY_LEVEL)
            middle = middles[first]
            if (first, second) not in bottoms:
                bottoms[(first, second)] = list(middle[second] or _EMPTY_LEVEL)
            bottom = bottoms[(first, second)]
            key = (first, second, third)
            if key not in leaves:
                leaves[key] = dict(bottom[third] or {})
            leaf = leaves[key]
            if vector is None:
                if leaf.pop(doc_id, None) is not None:
                    size -= 1
            else:
                if doc_id not in leaf:
                    size += 1
                leaf[doc_id] = vector
        for (first, second, third), leaf in leaves.items():
            bottoms[(first, second)][third] = leaf or None
        for (first, second), bottom in bottoms.items():
            middles[first][second] = tuple(bottom)
        for first, middle in middles.items():
            root[first] = tuple(middle)
        table = VectorTable()
        table._root = tuple(root)
        table._size = size
        return table


class NodeIndex:
    """Children by ``(parent, name)`` and nodes by doc id over one ``space.nodes``."""

    def __init__(self, nodes: Mapping[str, Any]) -> None:
        self.nodes = nodes
        self._children: dict[tuple[str | None, str], dict[str, None]] = {}
        self._docs: dict[str, dict[str, None]] = {}
        self._keys: dict[str, tuple[tuple[str | None, str], str | None]] = {}
        for node in nodes.values():
            self._add(node)

    def _add(self, node: Any) -> None:
        key = (node.parent, node.name)
        self._children.setdefault(key, {})[node.node_id] = None
        if node.doc_id:
            self._docs.setdefault(node.doc_id, {})[node.node_id] = None
        self._keys[node.node_id] = (key, node.doc_id)

    def _remove(self, node_id: str) -> None:
        entry = self._keys.pop(node_id, None)
        if entry is None:
            return
        key, doc_id = entry
        children = self._children.get(key)
        if children is not None:
            children.pop(node_id, None)
            if not children:
                del self._children[key]
        if doc_id:
            docs = self._docs.get(doc_id)
            if docs is not None:
                docs.pop(node_id, None)
                if not docs:
                    del self._docs[doc_id]

    def note(self, node: Any) -> None:
        """Re-register ``node`` after a local change of its row or a new node."""

        if self.nodes.get(node.node_id) is not node:
            self._remove(node.node_id)
            return
        if self._keys.get(node.node_id) == ((node.parent, node.name), node.doc_id):
            return
        self._remove(node.node_id)
        self._add(node)

    def children(self, parent: str | None, name: str) -> list[Any]:
        """The nodes now under ``parent`` named ``name``, deleted ones included."""

        found = []
        for node_id in self._children.get((parent, name), ()):
            node = self.nodes.get(node_id)
            if node is not None and node.parent == parent and node.name == name:
                found.append(node)
        return found

    def doc_nodes(self, doc_id: str) -> list[Any]:
        """Every node now naming ``doc_id``, in registration order."""

        found = []
        for node_id in self._docs.get(doc_id, ()):
            node = self.nodes.get(node_id)
            if node is not None and node.doc_id == doc_id:
                found.append(node)
        return found

    def doc_node(self, doc_id: str) -> Any:
        """The last live node of ``doc_id``, else its first deleted one."""

        chosen = None
        for node in self.doc_nodes(doc_id):
            if chosen is None or not node.deleted:
                chosen = node
        return chosen

    def view(self) -> tuple[dict[tuple[str | None, str], list[str]], dict[str, str]]:
        """Every non-empty lookup result, for comparing against a fresh build."""

        children = {}
        for parent, name in self._children:
            found = sorted(node.node_id for node in self.children(parent, name))
            if found:
                children[(parent, name)] = found
        docs = {}
        for doc_id in self._docs:
            node = self.doc_node(doc_id)
            if node is not None:
                docs[doc_id] = node.node_id
        return children, docs


def swap_content(document: _ContentDocument, fresh: Doc) -> None:
    """Install ``fresh`` as the instance behind a content wrapper, in place."""

    document.doc = fresh
    document.text = fresh.get("text", type=Text)


__all__ = [
    "BASE_TEXT_BYTES",
    "BASE_TEXT_ENTRIES",
    "BaseTexts",
    "CONTENT_CAPACITY",
    "DocRegistry",
    "EMPTY_VECTOR",
    "NodeIndex",
    "PUBLISH_QUEUE_LIMIT",
    "SealedContents",
    "StateTree",
    "VectorTable",
    "new_client_id",
    "parse_row",
    "swap_content",
]
