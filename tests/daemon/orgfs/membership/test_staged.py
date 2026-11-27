"""StagedDoc and state-vector helpers (design §3.3, §3.6)."""

from __future__ import annotations

import json

import pytest
from pycrdt import Array, Doc, Map, Text

from hyprial.daemon.impl.orgfs.membership import (
    StagedDoc,
    decode_vector,
    doc_kind,
    encode_vector,
    vector_advanced,
    vector_covers,
    vector_digest,
)


def _content_doc(text: str, *, client_id: int = 41) -> Doc:
    doc = Doc(client_id=client_id)
    with doc.transaction():
        doc.get("text", type=Text).insert(0, text)
        doc.get("__orgfs__", type=Map)["c"] = 1
        doc.get("__orgfs_authors__", type=Map)["user:alice"] = True
    return doc


def test_doc_kind_names_meta_tree_and_content() -> None:
    assert doc_kind("meta") == "meta"
    assert doc_kind("tree") == "tree"
    assert doc_kind("doc-00000000-0000-0000-0000-000000000001") == "content"


def test_vector_decode_is_order_independent_and_canonical() -> None:
    first = Doc(client_id=5)
    first.get("m", type=Map)["a"] = 1
    second = Doc(client_id=3)
    second.apply_update(first.get_update())
    second.get("m", type=Map)["b"] = 2
    first.apply_update(second.get_update())
    assert decode_vector(first.get_state()) == decode_vector(second.get_state())
    assert decode_vector(first.get_state()) == {5: 1, 3: 1}
    assert encode_vector({5: 1, 3: 1}) == encode_vector({3: 1, 5: 1})
    assert vector_digest(first.get_state()) == vector_digest(second.get_state())
    assert decode_vector(b"") == {}


def test_vector_decode_rejects_trailing_bytes() -> None:
    with pytest.raises(ValueError):
        decode_vector(b"\x01\x05\x01\x00")


def test_vector_covers_and_advanced_compare_decoded_clocks() -> None:
    low = encode_vector({1: 2})
    high = encode_vector({1: 3, 2: 1})
    assert vector_covers(high, low)
    assert not vector_covers(low, high)
    assert vector_advanced(low, high)
    assert not vector_advanced(high, high)
    assert not vector_advanced(high, encode_vector({1: 4}))


def test_staged_doc_never_produces_operations_when_read() -> None:
    staged = StagedDoc("tree")
    before = staged.vector()
    staged.materialize()
    assert decode_vector(staged.vector()) == decode_vector(before) == {}


def test_staged_content_reads_text_authors_and_clock() -> None:
    source = _content_doc("hello")
    staged = StagedDoc.from_state("content", source.get_update())
    value = staged.materialize()
    assert value.text == "hello"
    assert value.authors == frozenset({"user:alice"})
    assert value.clock == 1
    assert decode_vector(staged.vector()) == decode_vector(source.get_state())


def test_staged_meta_reads_space_and_members() -> None:
    source = Doc(client_id=9)
    with source.transaction():
        source.get("space", type=Map).update({"owner": "user:alice", "kind": "home"})
        source.get("members", type=Map)["user:alice"] = Map(
            {"mode": "rw", "addedBy": "user:alice", "addedAt": "t0"}
        )
        source.get("__orgfs__", type=Map)["c"] = 4
    value = StagedDoc.from_state("meta", source.get_update()).materialize()
    assert value.space == {"owner": "user:alice", "kind": "home"}
    assert value.members == {
        "user:alice": {"mode": "rw", "addedBy": "user:alice", "addedAt": "t0"}
    }
    assert value.clock == 4


def test_staged_tree_reads_raw_rows_paths_and_invalid_rows() -> None:
    source = Doc(client_id=11)
    rows = {
        "root": {"node_id": "root", "parent": None, "name": "", "kind": "dir",
                 "deleted": False, "author": "user:alice"},
        "a": {"node_id": "a", "parent": "root", "name": "docs", "kind": "dir",
              "deleted": False, "author": "user:alice"},
        "b": {"node_id": "b", "parent": "a", "name": "x.md", "kind": "doc",
              "doc_id": "doc-1", "deleted": False, "author": "user:bob"},
    }
    with source.transaction():
        nodes = source.get("nodes", type=Map)
        for node_id, row in rows.items():
            nodes[node_id] = json.dumps(row, sort_keys=True)
        nodes["bad"] = "{not json"
    value = StagedDoc.from_state("tree", source.get_update()).materialize()
    assert value.rows["b"]["author"] == "user:bob"
    assert value.paths["b"] == "docs/x.md"
    assert value.paths["root"] == ""
    assert "bad" in value.raw and "bad" not in value.rows
    assert value.raw["bad"] == "{not json"


def test_staged_apply_rejects_undecodable_update_with_value_error() -> None:
    staged = StagedDoc("content")
    with pytest.raises(ValueError):
        staged.apply(b"\x01\x02garbage")


def test_staged_reset_rebuilds_equal_vector_and_values() -> None:
    source = _content_doc("authoritative")
    staged = StagedDoc.from_state("content", source.get_update())
    polluter = _content_doc("other", client_id=77)
    staged.apply(polluter.get_update())
    assert staged.materialize().text != "authoritative"
    staged.reset(source.get_update())
    assert decode_vector(staged.vector()) == decode_vector(source.get_state())
    assert staged.materialize() == StagedDoc.from_state(
        "content", source.get_update()
    ).materialize()


def test_reset_from_authority_swaps_out_pending_structs() -> None:
    source = _content_doc("base", client_id=60)
    authority = Doc(client_id=61)
    authority.apply_update(source.get_update())
    start = source.get_state()
    source.get("text", type=Text).insert(0, "A")
    middle = source.get_state()
    predecessor = source.get_update(start)
    source.get("text", type=Text).insert(0, "B")
    authority.apply_update(source.get_update(middle))
    assert decode_vector(authority.get_state()) == decode_vector(start)
    staged = StagedDoc("content")
    fresh = staged.reset_from_authority(authority)
    assert fresh is not authority and fresh.client_id == 61
    assert decode_vector(fresh.get_state()) == decode_vector(staged.vector())
    fresh.apply_update(predecessor)
    staged.apply(predecessor)
    assert fresh.get("text", type=Text).to_py() == "Abase"
    assert staged.materialize().text == "Abase"
    authority.apply_update(predecessor)
    assert authority.get("text", type=Text).to_py() == "BAbase"


def test_staged_export_round_trips() -> None:
    source = _content_doc("round")
    staged = StagedDoc.from_state("content", source.get_update())
    again = StagedDoc.from_state("content", staged.export())
    assert again.materialize() == staged.materialize()


def test_materialize_tree_matches_tree_document_on_random_moves() -> None:
    import random

    from hyprial.daemon.impl.orgfs.document.model import TreeDocument
    from hyprial.daemon.impl.orgfs.membership import materialize_tree, tree_paths

    for seed in range(40):
        rng = random.Random(seed)
        tree = TreeDocument(client_id=1000 + seed)
        ids = ["root"]
        with tree.doc.transaction():
            for index in range(12):
                node_id = f"n{index}"
                parent = rng.choice(ids)
                tree.nodes[node_id] = json.dumps(
                    {"node_id": node_id, "parent": parent, "name": f"x{index}",
                     "kind": "dir", "deleted": False},
                    sort_keys=True,
                )
                ids.append(node_id)
            for step in range(20):
                tree.moves.append(json.dumps({
                    "ts": rng.randrange(5), "peer": rng.choice("ab"),
                    "node": rng.choice(ids[1:]), "oldParent": None,
                    "newParent": rng.choice(ids), "name": f"m{step}",
                }, sort_keys=True))
        expected = tree.materialize()
        staged = StagedDoc.from_state("tree", tree.get_update()).materialize()
        assert {key: dict(value) for key, value in staged.placed.items()} == expected
        rows = {key: json.loads(value) for key, value in tree.nodes.to_py().items()}
        moves = [json.loads(value) for value in tree.moves.to_py()]
        assert materialize_tree(rows, moves) == expected
        assert tree_paths(expected)["root"] == ""


def _cycle_tree_update() -> bytes:
    """Concurrent ``a -> b`` and ``b -> a`` moves merged: the rows' parents form a cycle."""

    def row(node_id: str, parent: str | None, name: str) -> str:
        return json.dumps({"node_id": node_id, "parent": parent, "name": name,
                           "kind": "dir", "deleted": False}, sort_keys=True)

    def move(ts: int, peer: str, node: str, old: str, new: str, name: str) -> str:
        return json.dumps({"ts": ts, "peer": peer, "node": node, "oldParent": old,
                           "newParent": new, "name": name}, sort_keys=True)

    base = Doc(client_id=1)
    with base.transaction():
        nodes = base.get("nodes", type=Map)
        nodes["root"] = row("root", None, "")
        nodes["x"] = row("x", "root", "x")
        nodes["a"] = row("a", "x", "a")
        nodes["b"] = row("b", "x", "b")
    left = Doc(client_id=2)
    right = Doc(client_id=3)
    for doc in (left, right):
        doc.apply_update(base.get_update())
    with left.transaction():
        left.get("nodes", type=Map)["a"] = row("a", "b", "a")
        left.get("moves", type=Array).append(move(1, "left", "a", "x", "b", "a"))
    with right.transaction():
        right.get("nodes", type=Map)["b"] = row("b", "a", "b")
        right.get("moves", type=Array).append(move(2, "right", "b", "x", "a", "b"))
    left.apply_update(right.get_update())
    return left.get_update()


def test_admission_and_facade_give_the_same_paths_for_a_merged_cycle() -> None:
    from hyprial.daemon.impl.orgfs.document.coordinator.registry import StateTree
    from hyprial.daemon.impl.orgfs.membership import tree_paths

    update = _cycle_tree_update()
    facade = StateTree(client_id=9, update=update).materialize()
    staged = StagedDoc.from_state("tree", update).materialize()
    assert {key: dict(value) for key, value in staged.placed.items()} == facade
    assert staged.paths == tree_paths(facade)
    assert staged.paths["b"] == "x/b"
    assert staged.paths["a"] == "x/b/a"


@pytest.mark.parametrize("loser", ["root", "x"])
def test_break_cycles_terminates_when_the_root_row_is_in_the_cycle(loser) -> None:
    from hyprial.daemon.impl.orgfs.membership import break_cycles

    placed = {
        "root": {"node_id": "root", "parent": "x", "name": ""},
        "x": {"node_id": "x", "parent": "root", "name": "x"},
    }
    moves = [{"ts": 5, "peer": "p", "node": loser, "oldParent": "root", "newParent": "x", "name": loser}]
    result = break_cycles(placed, moves)
    parents = {node: row["parent"] for node, row in result.items()}
    assert parents[loser] is None
    assert len({node for node, parent in parents.items() if parent is None}) == 1
