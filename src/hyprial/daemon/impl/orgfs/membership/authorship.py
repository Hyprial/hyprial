# Decision record
# - 2026-11-25 (C2, design §3.3): ``stamp_commit`` is the single local commit
#   helper.  It writes inside the caller's open transaction (pycrdt nests a
#   ``doc.transaction()`` into the active one), so the edit and the stamp
#   leave the document as one update.
# - The clock is stored as an integer; pycrdt returns map numbers as
#   ``float`` (0.14.9), so readers accept integral floats.
# - Authors live in their own root ``__orgfs_authors__`` (design §3.3, revised
#   after C2): a nested Map created concurrently on two replicas keeps only
#   one of the two Maps, a root type has no such race.
# - Meta commits that change the ``space`` root pass ``space_root=True`` and
#   the helper writes ``space.modifiedBy``.
# - Tree rows keep the facade's wire format (``json.dumps`` sorted, compact
#   separators, ``TreeDocument._wire_node``) and gain ``author`` and
#   ``deletedBatch``; ``document/model.py`` is not changed here.
# - 2026-11-25 (phase D P0, design §3.3 "the clock only when needed", C6
#   report §4-1): rewriting ``c`` on every commit leaves a 13-17 byte
#   tombstone per commit that merging cannot reclaim.  The helper now compares
#   the document's decoded state vector from before the edit with the vector
#   after the edit and the author fields, and advances ``c`` only when it did
#   not move (pure deletions, or an edit that changed nothing).  The vector
#   from before the edit is therefore a required argument of ``stamp_commit``;
#   ``local_commit`` is the context-manager form that records it on entry and
#   stamps on a clean exit.  Author fields are only rewritten when their value
#   changes, for the same reason.
# - 2026-11-25 (phase D P0, design §9 R9): a tree row may carry ``via``, the
#   local actor of the commit that last wrote the row.  The helper writes it
#   together with ``author``; ``via=None`` removes the key so a previous
#   writer's actor never outlives its author.  Admission does not judge it.
"""Author fields, tree-row author helpers and the commit clock."""

from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Any, Iterable, Iterator, Mapping

from pycrdt import Doc, Map

from hyprial.daemon.impl.orgfs.membership.staged import (
    AUTHORS_ROOT,
    CLOCK_KEY,
    ORGFS_ROOT,
    TREE_NODES_ROOT,
    DocKind,
    parse_tree_row,
    read_clock_value,
    vector_advanced,
)

ROW_AUTHOR = "author"
ROW_VIA = "via"
SPACE_ROOT = "space"
SPACE_MODIFIED_BY = "modifiedBy"
ROW_DELETED_BATCH = "deletedBatch"


def tree_row_wire(
    row: Mapping[str, Any], *, author: str | None = None, via: str | None = None
) -> str:
    """Encode a tree row in the facade's wire format, optionally with ``author`` and ``via``."""

    value = dict(row)
    if author is not None:
        value[ROW_AUTHOR] = author
    if via is not None:
        value[ROW_VIA] = via
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _decoded(wire: str) -> dict[str, Any]:
    value = parse_tree_row(wire)
    if value is None:
        raise ValueError("tree row is not a JSON object")
    return value


def _stamped_row(value: Mapping[str, Any], author: str, via: str | None) -> dict[str, Any]:
    stamped = dict(value)
    stamped[ROW_AUTHOR] = author
    if via:
        stamped[ROW_VIA] = via
    else:
        stamped.pop(ROW_VIA, None)
    return stamped


def with_row_author(wire: str, author: str, *, via: str | None = None) -> str:
    """Return ``wire`` with ``author`` set and ``via`` set (or removed when empty)."""

    if not author:
        raise ValueError("author is required")
    return tree_row_wire(_stamped_row(_decoded(wire), author, via))


def with_row_deleted_batch(wire: str, batch: str | None) -> str:
    """Return ``wire`` with ``deletedBatch`` set, or removed when ``batch`` is None."""

    value = _decoded(wire)
    if batch is None:
        value.pop(ROW_DELETED_BATCH, None)
    else:
        value[ROW_DELETED_BATCH] = batch
    return tree_row_wire(value)


def row_author(row: Mapping[str, Any]) -> str | None:
    """The row's last local author, or ``None`` when absent or malformed."""

    value = row.get(ROW_AUTHOR)
    return value if isinstance(value, str) and value else None


def row_via(row: Mapping[str, Any]) -> str | None:
    """The local actor of the row's last write, or ``None`` when absent or malformed."""

    value = row.get(ROW_VIA)
    return value if isinstance(value, str) and value else None


def row_deleted_batch(row: Mapping[str, Any]) -> str | None:
    """The row's deletion batch marker, or ``None``."""

    value = row.get(ROW_DELETED_BATCH)
    return value if isinstance(value, str) and value else None


def _orgfs_root(doc: Doc) -> Map:
    return doc.get(ORGFS_ROOT, type=Map)


def read_clock(doc: Doc) -> int:
    """The document's commit clock ``__orgfs__.c`` (``0`` when absent or malformed)."""

    value = read_clock_value((_orgfs_root(doc).to_py() or {}).get(CLOCK_KEY))
    return value or 0


def document_authors(doc: Doc) -> tuple[str, ...]:
    """Sorted keys of the content document's ``__orgfs_authors__``."""

    raw = doc.get(AUTHORS_ROOT, type=Map).to_py() or {}
    return tuple(sorted(str(key) for key, flag in raw.items() if flag is True))


def _advance_clock(doc: Doc) -> int:
    orgfs = _orgfs_root(doc)
    current = read_clock_value(orgfs.get(CLOCK_KEY)) or 0
    orgfs[CLOCK_KEY] = current + 1
    return current + 1


def _add_author(doc: Doc, author: str) -> None:
    authors = doc.get(AUTHORS_ROOT, type=Map)
    if authors.get(author) is not True:
        authors[author] = True


def _stamp_rows(doc: Doc, author: str, via: str | None, node_ids: Iterable[str]) -> None:
    nodes = doc.get(TREE_NODES_ROOT, type=Map)
    for node_id in node_ids:
        if node_id not in nodes:
            raise KeyError(node_id)
        wire = nodes[node_id]
        value = parse_tree_row(wire)
        if value is None:
            raise ValueError(f"tree row {node_id} is not a JSON object")
        stamped = _stamped_row(value, author, via)
        if stamped != value:
            nodes[node_id] = tree_row_wire(stamped)


def _check_stamp(kind: DocKind, author: str) -> None:
    if not author:
        raise ValueError("author is required")
    if kind not in ("meta", "tree", "content"):
        raise ValueError(f"unknown document kind: {kind}")


def stamp_commit(
    doc: Doc,
    kind: DocKind,
    author: str,
    *,
    before: bytes,
    via: str | None = None,
    tree_rows: Iterable[str] = (),
    space_root: bool = False,
) -> int:
    """Stamp one local commit on ``doc`` and return its clock.

    Call inside the transaction that made the edit, with ``before`` the
    document's state vector taken before the edit.  Content adds ``author``
    to ``__orgfs_authors__``; tree sets ``author`` and ``via`` on each row in
    ``tree_rows``; meta with ``space_root`` sets ``space.modifiedBy``.  The
    clock ``__orgfs__.c`` advances only when the decoded vector has not moved
    past ``before`` after all of that, so every commit advances the vector.
    """

    _check_stamp(kind, author)
    with doc.transaction():
        if kind == "tree":
            _stamp_rows(doc, author, via, tuple(tree_rows))
        elif kind == "content":
            _add_author(doc, author)
        elif space_root:
            space = doc.get(SPACE_ROOT, type=Map)
            if space.get(SPACE_MODIFIED_BY) != author:
                space[SPACE_MODIFIED_BY] = author
        if vector_advanced(before, doc.get_state()):
            return read_clock(doc)
        return _advance_clock(doc)


class CommitStamp:
    """What a ``local_commit`` stamps on exit; the body may add rows it wrote."""

    __slots__ = ("tree_rows", "space_root")

    def __init__(self, tree_rows: Iterable[str], space_root: bool) -> None:
        self.tree_rows: list[str] = list(tree_rows)
        self.space_root = space_root

    def touch(self, *node_ids: str) -> None:
        """Add tree rows written by the body to the rows to stamp."""

        for node_id in node_ids:
            if node_id not in self.tree_rows:
                self.tree_rows.append(node_id)


@contextmanager
def local_commit(
    doc: Doc,
    kind: DocKind,
    author: str,
    *,
    via: str | None = None,
    tree_rows: Iterable[str] = (),
    space_root: bool = False,
) -> Iterator[CommitStamp]:
    """Run the body as one local commit on ``doc`` and stamp it on a clean exit.

    ``with local_commit(doc, "tree", author, via=actor) as stamp:`` opens one
    transaction (nested in the caller's, if any), records the vector before
    the body, and after the body calls :func:`stamp_commit` with the rows
    named here or added through ``stamp.touch``.  An exception from the body
    propagates and nothing is stamped.
    """

    _check_stamp(kind, author)
    with doc.transaction():
        before = doc.get_state()
        stamp = CommitStamp(tree_rows, space_root)
        yield stamp
        stamp_commit(
            doc,
            kind,
            author,
            before=before,
            via=via,
            tree_rows=stamp.tree_rows,
            space_root=stamp.space_root,
        )
