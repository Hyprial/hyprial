# Decision record
# - 2026-11-25 (C2, design §3.6): a staged copy is a pycrdt ``Doc`` that only
#   ever receives ``apply_update``; reading a root through ``Doc.get`` does not
#   create an operation (measured on pycrdt 0.14.9), so materializing never
#   advances the vector.  No ``observe`` subscription is created, so the
#   copy carries no thread-bound ``Subscription``.
# - State vectors are compared only after decoding to ``{client: clock}``;
#   ``get_state()`` byte order is not stable across replicas (plan §10).
# - ``materialize_tree`` restates ``TreeDocument.materialize`` (move replay
#   ordered by ``(ts, peer, node)``, cycle-making moves skipped) as a pure
#   function so this package does not import the facade it will be wired
#   into in phase D; a test pins it to ``TreeDocument.materialize``.
#   Unlike that method it never raises on a malformed move ``ts``.
# - 2026-11-25 (F2, F1 report §7): ``break_cycles`` and ``_move_order`` moved
#   here verbatim from ``document/coordinator/registry.py``; ``materialize_tree``
#   ends with ``break_cycles``, so the admission's protected-path judgement and
#   the facade see the same path for a node of a merged move cycle.  Without
#   cycles the result still equals ``TreeDocument.materialize``.
# - One change to the moved ``break_cycles``: when the fallback parent still
#   reaches the loser (the root row itself is in the cycle, as a forged root
#   row with a parent makes it), the loser gets parent ``None`` instead of
#   ``root``.  The verbatim version re-parented the root to itself forever;
#   admission now materializes forged trees, so this input is reachable.
"""Staged, apply-only document copies and state-vector helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from pycrdt import Array, Doc, Map, Text

DocKind = Literal["meta", "tree", "content"]
ORGFS_ROOT = "__orgfs__"
CLOCK_KEY = "c"
AUTHORS_ROOT = "__orgfs_authors__"
TEXT_ROOT = "text"
TREE_NODES_ROOT = "nodes"
TREE_MOVES_ROOT = "moves"


def doc_kind(doc_id: str) -> DocKind:
    """Name the document family of ``doc_id``."""

    if doc_id == "meta":
        return "meta"
    if doc_id == "tree":
        return "tree"
    return "content"


def _read_var_uint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            break
    raise ValueError("invalid Yjs state vector")


def _write_var_uint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varuint cannot be negative")
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def decode_vector(value: bytes) -> dict[int, int]:
    """Decode a Yjs v1 state vector; raise ``ValueError`` when malformed."""

    if not value:
        return {}
    count, offset = _read_var_uint(value, 0)
    result: dict[int, int] = {}
    for _ in range(count):
        client, offset = _read_var_uint(value, offset)
        clock, offset = _read_var_uint(value, offset)
        result[client] = clock
    if offset != len(value):
        raise ValueError("invalid Yjs state vector")
    return result


def encode_vector(value: Mapping[int, int]) -> bytes:
    """Encode clocks canonically: clients ascending, zero clocks dropped."""

    items = sorted((int(client), int(clock)) for client, clock in value.items() if clock > 0)
    parts = [_write_var_uint(len(items))]
    for client, clock in items:
        parts.append(_write_var_uint(client))
        parts.append(_write_var_uint(clock))
    return b"".join(parts)


def vector_digest(value: bytes) -> str:
    """SHA-256 of the canonical encoding of a state vector."""

    return hashlib.sha256(encode_vector(decode_vector(value))).hexdigest()


def vector_covers(actual: bytes, required: bytes) -> bool:
    """Whether ``actual`` has every clock of ``required``."""

    have = decode_vector(actual)
    return all(have.get(client, 0) >= clock for client, clock in decode_vector(required).items())


def vector_advanced(before: bytes, after: bytes) -> bool:
    """Whether ``after`` covers ``before`` and is strictly ahead of it."""

    old = decode_vector(before)
    new = decode_vector(after)
    if any(new.get(client, 0) < clock for client, clock in old.items()):
        return False
    return any(clock > old.get(client, 0) for client, clock in new.items())


def read_clock_value(raw: object) -> int | None:
    """Return an integral clock value, ``0`` when absent, ``None`` when malformed."""

    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if isinstance(raw, float) and not raw.is_integer():
        return None
    return int(raw)


def _root_dict(doc: Doc, name: str) -> dict[str, Any]:
    try:
        value = doc.get(name, type=Map).to_py()
    except (TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _orgfs_clock(doc: Doc) -> int | None:
    return read_clock_value(_root_dict(doc, ORGFS_ROOT).get(CLOCK_KEY))


@dataclass(frozen=True)
class MetaValue:
    """Materialized meta document."""

    space: Mapping[str, Any]
    members: Mapping[str, Any]
    clock: int | None


@dataclass(frozen=True)
class TreeValue:
    """Materialized tree document.

    ``raw`` holds every ``nodes`` entry as stored; ``rows`` the entries that
    parse as JSON objects; ``placed`` the rows after move replay; ``paths``
    the slash path of every placed row.
    """

    raw: Mapping[str, object]
    rows: Mapping[str, Mapping[str, Any]]
    placed: Mapping[str, Mapping[str, Any]]
    paths: Mapping[str, str]
    clock: int | None


@dataclass(frozen=True)
class ContentValue:
    """Materialized content document."""

    text: str
    authors: frozenset[str]
    authors_valid: bool
    clock: int | None
    extra_roots: frozenset[str] = field(default_factory=frozenset)


def parse_tree_row(raw: object) -> dict[str, Any] | None:
    """Parse one stored tree row, or ``None`` when it is not a JSON object."""

    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _move_order(move: Mapping[str, Any]) -> tuple[int, str, str]:
    try:
        ts = int(move.get("ts", 0))
    except (TypeError, ValueError):
        ts = 0
    return ts, str(move.get("peer", "")), str(move.get("node", ""))


def break_cycles(
    placed: dict[str, dict[str, Any]], moves: list[Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Detach the losing member of every parent cycle left in ``placed``."""

    latest: dict[str, Mapping[str, Any]] = {}
    for move in sorted(moves, key=_move_order):
        latest[str(move.get("node", ""))] = move

    def cycle_of(start: str) -> list[str]:
        path: list[str] = []
        index: dict[str, int] = {}
        cursor: object = start
        while isinstance(cursor, str) and cursor in placed and cursor not in index:
            index[cursor] = len(path)
            path.append(cursor)
            cursor = placed[cursor].get("parent")
        return path[index[cursor]:] if isinstance(cursor, str) and cursor in index else []

    def reaches(node_id: str, parent: object) -> bool:
        seen: set[str] = set()
        cursor = parent
        while isinstance(cursor, str) and cursor in placed and cursor not in seen:
            if cursor == node_id:
                return True
            seen.add(cursor)
            cursor = placed[cursor].get("parent")
        return isinstance(cursor, str) and cursor in seen

    for start in sorted(placed):
        members = cycle_of(start)
        while members:
            loser = max(
                members,
                key=lambda node: (
                    (1, *_move_order(latest[node])) if node in latest else (0, 0, "", ""),
                    node,
                ),
            )
            target = latest[loser].get("oldParent") if loser in latest else None
            if not isinstance(target, str) or target not in placed or reaches(loser, target):
                target = "root"
            if reaches(loser, target):
                target = None
            placed[loser]["parent"] = target
            members = cycle_of(start)
    return placed


def materialize_tree(
    rows: Mapping[str, Mapping[str, Any]], moves: list[Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Replay ``moves`` over ``rows`` exactly as ``TreeDocument.materialize``."""

    nodes = {key: dict(value) for key, value in rows.items()}
    parents = {key: value.get("parent") for key, value in nodes.items()}
    names = {key: value.get("name", "") for key, value in nodes.items()}

    def cycle(node_id: str, parent: object) -> bool:
        seen: set[object] = set()
        cursor = parent
        while cursor is not None:
            if cursor == node_id or cursor in seen:
                return True
            seen.add(cursor)
            cursor = parents.get(cursor) if isinstance(cursor, str) else None
        return False

    for move in sorted(moves, key=_move_order):
        node_id = str(move.get("node", ""))
        parent = move.get("newParent")
        if node_id not in nodes or parent is not None and parent not in nodes:
            continue
        if cycle(node_id, parent):
            continue
        parents[node_id] = parent
        if "name" in move:
            names[node_id] = str(move["name"])
    for node_id, node in nodes.items():
        node["parent"] = parents.get(node_id)
        node["name"] = names.get(node_id, node.get("name", ""))
    return break_cycles(nodes, moves)


def tree_paths(placed: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """Slash path of every placed row, walking parents as the facade does."""

    paths: dict[str, str] = {}
    for node_id in placed:
        if node_id == "root":
            paths[node_id] = ""
            continue
        pieces: list[str] = []
        seen: set[str] = set()
        cursor: object = node_id
        while isinstance(cursor, str) and cursor != "root":
            if cursor in seen or cursor not in placed:
                break
            seen.add(cursor)
            item = placed[cursor]
            pieces.append(str(item.get("name", "")))
            cursor = item.get("parent")
        paths[node_id] = "/".join(reversed(pieces))
    return paths


def _tree_value(doc: Doc) -> TreeValue:
    raw = _root_dict(doc, TREE_NODES_ROOT)
    rows: dict[str, dict[str, Any]] = {}
    for node_id, value in raw.items():
        parsed = parse_tree_row(value)
        if parsed is not None:
            rows[str(node_id)] = parsed
    try:
        stored_moves = doc.get(TREE_MOVES_ROOT, type=Array).to_py() or []
    except (TypeError, ValueError):
        stored_moves = []
    moves = [parsed for parsed in map(parse_tree_row, stored_moves) if parsed is not None]
    placed = materialize_tree(rows, moves)
    return TreeValue(
        raw=dict(raw),
        rows=rows,
        placed=placed,
        paths=tree_paths(placed),
        clock=_orgfs_clock(doc),
    )


def _content_value(doc: Doc) -> ContentValue:
    try:
        text = doc.get(TEXT_ROOT, type=Text).to_py() or ""
    except (TypeError, ValueError):
        text = ""
    orgfs = _root_dict(doc, ORGFS_ROOT)
    raw_authors = _root_dict(doc, AUTHORS_ROOT)
    authors = frozenset(str(key) for key, flag in raw_authors.items() if flag is True)
    valid = all(flag is True for flag in raw_authors.values())
    return ContentValue(
        text=text,
        authors=authors,
        authors_valid=valid,
        clock=read_clock_value(orgfs.get(CLOCK_KEY)),
    )


def _meta_value(doc: Doc) -> MetaValue:
    return MetaValue(
        space=_root_dict(doc, "space"),
        members=_root_dict(doc, "members"),
        clock=_orgfs_clock(doc),
    )


def materialize_doc(kind: DocKind, doc: Doc) -> MetaValue | TreeValue | ContentValue:
    """Read the materialized value of ``doc`` for its document family."""

    if kind == "meta":
        return _meta_value(doc)
    if kind == "tree":
        return _tree_value(doc)
    return _content_value(doc)


class StagedDoc:
    """An apply-only copy of one document used for admission.

    It never produces an operation: the only mutation is ``apply``.  The
    coordinator keeps it equal to the authoritative instance and calls
    ``reset`` with the authoritative state after a ``Rejected`` or
    ``Pending`` admission.
    """

    __slots__ = ("kind", "_doc")

    def __init__(self, kind: DocKind, state: bytes | None = None) -> None:
        if kind not in ("meta", "tree", "content"):
            raise ValueError(f"unknown document kind: {kind}")
        self.kind: DocKind = kind
        self._doc = Doc()
        if state:
            self.apply(state)

    @classmethod
    def from_state(cls, kind: DocKind, state: bytes | None) -> "StagedDoc":
        """Build a staged copy from one full-state update."""

        return cls(kind, state)

    def vector(self) -> bytes:
        """The copy's raw state vector (decode before comparing)."""

        return self._doc.get_state()

    def apply(self, update: bytes) -> None:
        """Apply ``update``; an undecodable update raises ``ValueError``."""

        try:
            self._doc.apply_update(bytes(update))
        except (TypeError, ValueError) as error:
            raise ValueError(str(error)) from error

    def materialize(self) -> MetaValue | TreeValue | ContentValue:
        """The materialized value for this copy's document family."""

        return materialize_doc(self.kind, self._doc)

    def export(self) -> bytes:
        """The copy's full state as one update."""

        return self._doc.get_update()

    def reset(self, state: bytes | None) -> None:
        """Rebuild the copy from the authoritative full state."""

        self._doc = Doc()
        if state:
            self.apply(state)

    def reset_from_authority(self, authority: Doc) -> Doc:
        """Export ``authority``, rebuild this copy from the export, return the new authority.

        Design §3.6 "export swaps the instance": whenever the coordinator
        exports the authoritative state it must replace the authoritative
        ``Doc`` with one loaded from that same export under the same client
        id, so structs stuck in the old instance's in-memory pending set
        vanish from memory, disk and the staged copy at once.  The caller
        installs the returned ``Doc`` as its authoritative instance.
        """

        export = authority.get_update()
        self.reset(export)
        fresh = Doc(client_id=authority.client_id)
        fresh.apply_update(export)
        return fresh
