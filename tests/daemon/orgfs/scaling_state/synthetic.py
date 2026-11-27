"""Deterministic Yjs documents and synthetic spaces for the scaling tests.

Decisions (C6, 2026-11-25):

* Tree rows copy the shape written by ``document/model.py``
  ``TreeDocument._wire_node`` / ``record_node``: a ``nodes`` Map of
  sorted-key compact JSON strings, a ``moves`` Array of JSON move records,
  plus the design's new ``author`` (§3.3) and ``deletedBatch`` (§6) keys.
  The generator copies the shape rather than importing the legacy model so it
  keeps working after the legacy model is deleted (phase E).
* Content documents are one ``Text`` named ``text`` plus the ``__orgfs__`` Map
  of §3.3 (``authors``, ``c``), written by a fixed client id so builds are
  reproducible.
* Nothing here asserts; helpers only build bytes.
"""

from __future__ import annotations

import base64
import json
from typing import Iterator

from pycrdt import Array, Doc, Map, Text

AUTHOR = "user:scale"


def doc_id(index: int) -> str:
    return f"doc-{index:08d}-0000-4000-8000-{index:012d}"


def content_update(index: int, text: str = "hello", *, client_id: int = 7) -> bytes:
    doc = Doc(client_id=client_id + index)
    body = doc.get("text", type=Text)
    meta = doc.get("__orgfs__", type=Map)
    with doc.transaction():
        body += f"{text} {index}"
        meta["c"] = 1
        meta["authors"] = Map({AUTHOR: True})
    return doc.get_update()


def content_updates(count: int) -> Iterator[tuple[str, bytes]]:
    for index in range(count):
        yield doc_id(index), content_update(index)


def tree_row(node_id: str, parent: str | None, name: str, kind: str, index: int = 0) -> str:
    row: dict[str, object] = {
        "node_id": node_id,
        "parent": parent,
        "name": name,
        "kind": kind,
        "doc_id": doc_id(index) if kind == "doc" else None,
        "blob_hash": None,
        "deleted": False,
        "author": AUTHOR,
        "deletedBatch": None,
    }
    if kind == "doc":
        frontier = Doc(client_id=7 + index)
        with frontier.transaction():
            frontier.get("text", type=Text).insert(0, "x")
        row["contentFrontier"] = base64.b64encode(frontier.get_state()).decode("ascii")
        row["refSize"] = 40 + index % 900
        row["refSha256"] = f"{index:064x}"[-64:]
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def synthetic_tree_doc(node_count: int, *, client_id: int = 11) -> Doc:
    """A tree with ``node_count`` rows: half directories, half document rows."""

    doc = Doc(client_id=client_id)
    nodes = doc.get("nodes", type=Map)
    moves = doc.get("moves", type=Array)
    meta = doc.get("__orgfs__", type=Map)
    docs = node_count // 2
    dirs = node_count - docs
    with doc.transaction():
        meta["c"] = 1
        nodes["root"] = tree_row("root", None, "", "dir")
        for d in range(1, dirs):
            parent = "root" if d < 100 else f"dir-{d % 99 + 1:08d}"
            nodes[f"dir-{d:08d}"] = tree_row(f"dir-{d:08d}", parent, f"folder-{d}", "dir")
        for i in range(docs):
            parent = f"dir-{i % (dirs - 1) + 1:08d}"
            nodes[f"node-{i:08d}"] = tree_row(f"node-{i:08d}", parent, f"note-{i}.md", "doc", i)
        for i in range(0, docs, 50):
            moves.append(
                json.dumps(
                    {
                        "ts": 1_700_000_000_000 + i,
                        "peer": "node-a",
                        "node": f"node-{i:08d}",
                        "oldParent": "root",
                        "newParent": f"dir-{i % (dirs - 1) + 1:08d}",
                        "name": f"note-{i}.md",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
    return doc


def synthetic_meta_doc(*, client_id: int = 3) -> Doc:
    doc = Doc(client_id=client_id)
    space = doc.get("space", type=Map)
    members = doc.get("members", type=Map)
    stamp = doc.get("__orgfs__", type=Map)
    with doc.transaction():
        space["owner"] = AUTHOR
        space["kind"] = "ordinary"
        members[AUTHOR] = Map({"mode": "rw", "addedBy": AUTHOR, "addedAt": "2026-11-25T00:00:00Z"})
        stamp["c"] = 1
    return doc


def edit_tree_row(doc: Doc, node_id: str, name: str) -> bytes:
    """Rename one tree row the way a local write would; return the delta update."""

    nodes = doc.get("nodes", type=Map)
    meta = doc.get("__orgfs__", type=Map)
    before = doc.get_state()
    with doc.transaction():
        row = json.loads(nodes[node_id])
        row["name"] = name
        nodes[node_id] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        meta["c"] = int(meta["c"]) + 1
    return doc.get_update(before)


def decode_state_vector(raw: bytes) -> dict[int, int]:
    """Decode a lib0 v1 state vector into ``{client: clock}`` (bytes order is not stable)."""

    def varuint(position: int) -> tuple[int, int]:
        value = shift = 0
        while True:
            byte = raw[position]
            position += 1
            value |= (byte & 0x7F) << shift
            if byte < 0x80:
                return value, position
            shift += 7

    count, position = varuint(0)
    result: dict[int, int] = {}
    for _ in range(count):
        client, position = varuint(position)
        clock, position = varuint(position)
        result[client] = clock
    return result
