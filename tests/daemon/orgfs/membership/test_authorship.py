"""Author fields and the commit clock (design §3.3, §9 R9)."""

from __future__ import annotations

import json

import pytest
from pycrdt import Doc, Map, Text

from hyprial.daemon.impl.orgfs.membership import (
    decode_vector,
    document_authors,
    local_commit,
    read_clock,
    row_author,
    row_deleted_batch,
    row_via,
    stamp_commit,
    tree_row_wire,
    vector_advanced,
    with_row_author,
    with_row_deleted_batch,
)


def _row(node_id: str, **extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "node_id": node_id, "parent": "root", "name": node_id, "kind": "dir",
        "doc_id": None, "blob_hash": None, "deleted": False,
    }
    row.update(extra)
    return row


def _rows(doc: Doc) -> dict[str, dict[str, object]]:
    return {key: json.loads(value) for key, value in doc.get("nodes", type=Map).to_py().items()}


def test_content_commit_adds_author_without_a_clock_when_the_vector_advances() -> None:
    doc = Doc(client_id=21)
    before = doc.get_state()
    with local_commit(doc, "content", "user:alice"):
        doc.get("text", type=Text).insert(0, "hello")
    assert read_clock(doc) == 0
    assert "c" not in doc.get("__orgfs__", type=Map).to_py()
    assert document_authors(doc) == ("user:alice",)
    assert vector_advanced(before, doc.get_state())


def test_stamp_commit_in_the_callers_transaction_returns_the_clock() -> None:
    doc = Doc(client_id=21)
    with doc.transaction():
        before = doc.get_state()
        doc.get("text", type=Text).insert(0, "hello")
        assert stamp_commit(doc, "content", "user:alice", before=before) == 0
    with doc.transaction():
        before = doc.get_state()
        del doc.get("text", type=Text)[0:1]
        assert stamp_commit(doc, "content", "user:alice", before=before) == 1
    assert read_clock(doc) == 1


def test_content_authors_only_grow_and_are_sorted() -> None:
    doc = Doc(client_id=22)
    for author in ("user:bob", "user:alice", "user:bob"):
        with local_commit(doc, "content", author):
            pass
    assert document_authors(doc) == ("user:alice", "user:bob")
    assert read_clock(doc) == 1


def test_pure_delete_commit_advances_the_vector_through_the_clock() -> None:
    doc = Doc(client_id=23)
    text = doc.get("text", type=Text)
    with local_commit(doc, "content", "user:alice"):
        text.insert(0, "hello")
    before = doc.get_state()
    with doc.transaction():
        del text[0:2]
    assert decode_vector(doc.get_state()) == decode_vector(before)
    with local_commit(doc, "content", "user:alice"):
        del text[0:1]
    assert read_clock(doc) == 1
    assert vector_advanced(before, doc.get_state())


def test_a_commit_that_changes_nothing_still_advances_the_vector() -> None:
    doc = Doc(client_id=28)
    with local_commit(doc, "content", "user:alice"):
        doc.get("text", type=Text).insert(0, "x")
    for expected in (1, 2):
        before = doc.get_state()
        with local_commit(doc, "content", "user:alice"):
            pass
        assert read_clock(doc) == expected
        assert vector_advanced(before, doc.get_state())


def test_clock_writes_scale_with_pure_deletes_only() -> None:
    doc = Doc(client_id=29)
    text = doc.get("text", type=Text)
    for index in range(20):
        with local_commit(doc, "content", "user:alice"):
            text.insert(len(text), f"line {index}\n")
    assert read_clock(doc) == 0
    for _ in range(3):
        with local_commit(doc, "content", "user:alice"):
            del text[0:1]
    assert read_clock(doc) == 3


def test_an_exception_in_the_body_stamps_nothing() -> None:
    doc = Doc(client_id=30)
    with pytest.raises(RuntimeError):
        with local_commit(doc, "content", "user:alice"):
            raise RuntimeError("boom")
    assert document_authors(doc) == ()
    assert read_clock(doc) == 0


def test_tree_commit_sets_author_of_touched_rows_only() -> None:
    doc = Doc(client_id=24)
    nodes = doc.get("nodes", type=Map)
    with local_commit(doc, "tree", "user:alice", tree_rows=("a", "b")):
        nodes["a"] = tree_row_wire(_row("a"), author="user:alice")
        nodes["b"] = tree_row_wire(_row("b"), author="user:alice")
    before = doc.get_state()
    with local_commit(doc, "tree", "user:bob") as stamp:
        nodes["a"] = json.dumps(_row("a", name="renamed"))
        stamp.touch("a")
    rows = _rows(doc)
    assert rows["a"]["author"] == "user:bob"
    assert rows["a"]["name"] == "renamed"
    assert rows["b"]["author"] == "user:alice"
    assert read_clock(doc) == 0
    assert vector_advanced(before, doc.get_state())


def test_tree_commit_writes_via_with_author_and_drops_a_stale_one() -> None:
    doc = Doc(client_id=34)
    nodes = doc.get("nodes", type=Map)
    with local_commit(doc, "tree", "user:alice", via="agent:alice/helper", tree_rows=("a",)):
        nodes["a"] = tree_row_wire(_row("a"))
    row = _rows(doc)["a"]
    assert (row_author(row), row_via(row)) == ("user:alice", "agent:alice/helper")
    with local_commit(doc, "tree", "user:bob", tree_rows=("a",)):
        nodes["a"] = tree_row_wire(_row("a", name="b"), author="user:alice", via="agent:alice/helper")
    row = _rows(doc)["a"]
    assert (row_author(row), row_via(row)) == ("user:bob", None)
    assert "via" not in row


def test_restamping_an_unchanged_row_uses_the_clock() -> None:
    doc = Doc(client_id=35)
    nodes = doc.get("nodes", type=Map)
    with local_commit(doc, "tree", "user:alice", via="agent:x", tree_rows=("a",)):
        nodes["a"] = tree_row_wire(_row("a"))
    before = doc.get_state()
    with local_commit(doc, "tree", "user:alice", via="agent:x", tree_rows=("a",)):
        pass
    assert read_clock(doc) == 1
    assert vector_advanced(before, doc.get_state())
    with local_commit(doc, "tree", "user:alice", via="agent:y", tree_rows=("a",)):
        pass
    assert read_clock(doc) == 1
    assert row_via(_rows(doc)["a"]) == "agent:y"


def test_tree_commit_rejects_unknown_row() -> None:
    doc = Doc(client_id=25)
    with pytest.raises(KeyError):
        with local_commit(doc, "tree", "user:alice", tree_rows=("missing",)):
            pass


def test_meta_commit_advances_the_vector() -> None:
    doc = Doc(client_id=26)
    before = doc.get_state()
    with local_commit(doc, "meta", "user:alice"):
        doc.get("space", type=Map)["kind"] = "home"
    assert read_clock(doc) == 0
    assert vector_advanced(before, doc.get_state())
    before = doc.get_state()
    with local_commit(doc, "meta", "user:alice"):
        del doc.get("space", type=Map)["kind"]
    assert read_clock(doc) == 1
    assert vector_advanced(before, doc.get_state())


def test_commit_requires_an_author_and_a_known_kind() -> None:
    with pytest.raises(ValueError):
        stamp_commit(Doc(), "content", "", before=b"\x00")
    with pytest.raises(ValueError):
        with local_commit(Doc(), "content", ""):
            pass
    with pytest.raises(ValueError):
        with local_commit(Doc(), "blob", "user:alice"):  # type: ignore[arg-type]
            pass


def test_row_author_via_and_deleted_batch_wire_helpers() -> None:
    wire = tree_row_wire(_row("a"), author="user:alice", via="agent:a")
    assert json.loads(wire)["author"] == "user:alice"
    assert json.loads(wire)["via"] == "agent:a"
    assert wire == json.dumps(json.loads(wire), sort_keys=True, separators=(",", ":"))
    updated = with_row_author(wire, "user:bob")
    assert row_author(json.loads(updated)) == "user:bob"
    assert row_via(json.loads(updated)) is None
    assert row_via(json.loads(with_row_author(wire, "user:bob", via="agent:b"))) == "agent:b"
    batch = with_row_deleted_batch(updated, "batch-1")
    parsed = json.loads(batch)
    assert row_deleted_batch(parsed) == "batch-1"
    assert row_author(parsed) == "user:bob"
    cleared = json.loads(with_row_deleted_batch(batch, None))
    assert row_deleted_batch(cleared) is None
    assert "deletedBatch" not in cleared
    assert row_author({"author": ""}) is None
    assert row_author({"author": 3}) is None
    assert row_via({"via": ""}) is None
    assert row_via({"via": 3}) is None
    assert row_deleted_batch({"deletedBatch": 3}) is None


def test_readers_on_documents_without_reserved_root() -> None:
    doc = Doc()
    assert read_clock(doc) == 0
    assert document_authors(doc) == ()


def test_meta_commit_on_the_space_root_records_modified_by() -> None:
    doc = Doc(client_id=27)
    with local_commit(doc, "meta", "user:owner", space_root=True):
        doc.get("space", type=Map)["name"] = "x"
    assert doc.get("space", type=Map)["modifiedBy"] == "user:owner"
    with local_commit(doc, "meta", "user:other"):
        doc.get("members", type=Map)["user:a"] = Map({"mode": "rw"})
    assert doc.get("space", type=Map)["modifiedBy"] == "user:owner"
    with local_commit(doc, "meta", "user:owner") as stamp:
        doc.get("space", type=Map)["name"] = "y"
        stamp.space_root = True
    assert doc.get("space", type=Map)["modifiedBy"] == "user:owner"


def test_concurrent_first_writes_keep_both_authors() -> None:
    first = Doc(client_id=71)
    second = Doc(client_id=72)
    with local_commit(first, "content", "user:alice"):
        first.get("text", type=Text).insert(0, "a")
    with local_commit(second, "content", "user:bob"):
        second.get("text", type=Text).insert(0, "b")
    first.apply_update(second.get_update())
    second.apply_update(first.get_update())
    assert document_authors(first) == document_authors(second) == ("user:alice", "user:bob")
