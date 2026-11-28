"""Content rows of the §3.6 table (C7 §5.3 item 2, review must-change 5)."""

from __future__ import annotations

import pytest
from pycrdt import Map

from hyprial.daemon.impl.orgfs.membership import (
    ORGFS_TEXT_MAX,
    Accepted,
    Pending,
    Rejected,
    ResultStateAdmission,
)
from tests.daemon.orgfs.membership.support import (
    OWNER,
    Frame,
    Replica,
    deliver,
    forge_content,
    forge_text_delete,
    forge_update,
)

ALICE = "user:alice"
BOB = "user:bob"
DEVICE = "directory/devices/alice/laptop.json"
LEAVE = "directory/leaves/alice.json"


@pytest.fixture
def admission() -> ResultStateAdmission:
    return ResultStateAdmission()


@pytest.fixture
def origin() -> Replica:
    replica = Replica.create(members={ALICE: "rw", BOB: "rw"})
    replica.makedirs("directory/devices", author=OWNER)
    replica.makedirs("directory/leaves", author=OWNER)
    replica.makedirs("directory/devices/alice", author=ALICE)
    replica.write_text(DEVICE, '{"deviceId":"laptop"}', author=ALICE)
    replica.write_text(LEAVE, '{"user":"alice"}', author=ALICE)
    replica.makedirs("notes", author=BOB)
    replica.write_text("notes/a.md", "hello", author=BOB)
    return replica


def _doc_id(replica: Replica, path: str) -> str:
    return str(replica.tree_value().rows[replica.resolve(path)]["doc_id"])


def _rejected(result: object, code: str) -> None:
    assert isinstance(result, Rejected), result
    assert result.code == code, result


def test_path_owner_edits_protected_content(admission, origin) -> None:
    receiver = origin.clone(90)
    content = origin.write_text(DEVICE, '{"deviceId":"laptop-2"}', author=ALICE)[1]
    assert deliver(admission, receiver, content) == Accepted()


def test_other_member_edit_of_protected_content_is_rejected(admission, origin) -> None:
    receiver = origin.clone(90)
    bob = origin.clone(3)
    content = bob.write_text(LEAVE, '{"user":"alice","forged":true}', author=BOB)[1]
    _rejected(deliver(admission, receiver, content), "protected-path")
    assert receiver.staged(content.doc_id).materialize().text == '{"user":"alice"}'


def test_owner_edit_of_a_device_record_is_rejected(admission, origin) -> None:
    receiver = origin.clone(90)
    owner = origin.clone(5)
    _rejected(deliver(admission, receiver, owner.write_text(DEVICE, "{}", author=OWNER)[1]), "protected-path")


def test_any_member_edits_ordinary_content(admission, origin) -> None:
    receiver = origin.clone(90)
    alice = origin.clone(2)
    assert deliver(admission, receiver, alice.write_text("notes/a.md", "by alice", author=ALICE)[1]) == Accepted()
    assert receiver.staged(_doc_id(origin, "notes/a.md")).materialize().authors == {ALICE, BOB}


def test_authors_must_not_shrink(admission, origin) -> None:
    receiver = origin.clone(90)
    doc_id = _doc_id(origin, "notes/a.md")

    def replace_authors(doc, author) -> None:
        authors = doc.get("__orgfs_authors__", type=Map)
        del authors[BOB]
        authors[ALICE] = True
        doc.get("__orgfs__", type=Map)["c"] = 9

    frame = forge_update(doc_id, replace_authors, author=ALICE, client_id=2, base=origin.state(doc_id))
    _rejected(deliver(admission, receiver, frame), "protected-path")


def test_malformed_authors_are_rejected(admission, origin) -> None:
    receiver = origin.clone(90)
    doc_id = _doc_id(origin, "notes/a.md")

    def break_authors(doc, author) -> None:
        doc.get("__orgfs_authors__", type=Map)[ALICE] = "yes"
        doc.get("__orgfs__", type=Map)["c"] = 9

    frame = forge_update(doc_id, break_authors, author=ALICE, client_id=2, base=origin.state(doc_id))
    _rejected(deliver(admission, receiver, frame), "invalid-argument")


def test_pure_delete_text_change_that_does_not_advance_the_vector_is_rejected(admission, origin) -> None:
    receiver = origin.clone(90)
    doc_id = _doc_id(origin, "notes/a.md")
    frame = forge_text_delete(doc_id, origin.state(doc_id), 0, 2, client_id=3)
    _rejected(deliver(admission, receiver, frame), "invalid-argument")
    assert receiver.staged(doc_id).materialize().text == "hello"


def test_insert_without_clock_still_advances_and_is_accepted(admission, origin) -> None:
    receiver = origin.clone(90)
    doc_id = _doc_id(origin, "notes/a.md")
    frame = forge_content(doc_id, origin.state(doc_id), "silent", author=BOB, clock=False, client_id=3)
    assert deliver(admission, receiver, frame) == Accepted()


def test_honest_pure_delete_edit_is_accepted(admission, origin) -> None:
    receiver = origin.clone(90)
    alice = origin.clone(2)
    content = alice.write_text("notes/a.md", "hel", author=ALICE)[1]
    assert deliver(admission, receiver, content) == Accepted()
    assert receiver.staged(content.doc_id).materialize().text == "hel"


def test_design_gap_text_change_without_new_author_is_accepted(admission, origin) -> None:
    receiver = origin.clone(90)
    doc_id = _doc_id(origin, LEAVE)
    frame = forge_content(doc_id, origin.state(doc_id), "forged", author=None, clock=True, client_id=3)
    assert deliver(admission, receiver, frame) == Accepted()


def test_text_over_limit_is_rejected_too_large(admission, origin) -> None:
    receiver = origin.clone(90)
    doc_id = _doc_id(origin, "notes/a.md")
    big = "x" * (ORGFS_TEXT_MAX + 1)
    frame = forge_content(doc_id, origin.state(doc_id), big, author=BOB, clock=True, client_id=3)
    _rejected(deliver(admission, receiver, frame), "too-large")


def test_undecodable_update_is_rejected_invalid_argument(admission, origin) -> None:
    receiver = origin.clone(90)
    doc_id = _doc_id(origin, "notes/a.md")
    _rejected(deliver(admission, receiver, Frame(doc_id, b"\x05garbage", b"")), "invalid-argument")
    for doc in ("meta", "tree"):
        _rejected(deliver(admission, receiver, Frame(doc, b"\x05garbage", b"")), "invalid-argument")


def test_content_before_its_tree_row_is_pending_unknown_doc(admission, origin) -> None:
    receiver = origin.clone(90)
    alice = origin.clone(2)
    tree, content = alice.write_text("notes/new.md", "fresh", author=ALICE)
    assert deliver(admission, receiver, content) == Pending("unknown-doc")
    assert content.doc_id not in receiver.docs
    assert deliver(admission, receiver, tree) == Accepted()
    assert deliver(admission, receiver, content) == Accepted()


@pytest.mark.parametrize("content_first", [True, False])
def test_protected_verdict_does_not_depend_on_arrival_order(admission, origin, content_first) -> None:
    receiver = origin.clone(90)
    bob = origin.clone(3)
    tree, content = bob.write_text("directory/devices/alice/evil.json", "{}", author=BOB)
    if content_first:
        assert deliver(admission, receiver, content) == Pending("unknown-doc")
    _rejected(deliver(admission, receiver, tree), "protected-path")
    assert deliver(admission, receiver, content) == Pending("unknown-doc")
    honest_tree, honest_content = origin.clone(2).write_text(
        "directory/devices/alice/evil.json", "{}", author=ALICE
    )
    assert deliver(admission, receiver, honest_tree) == Accepted()
    _rejected(deliver(admission, receiver, content), "protected-path")


def test_text_limit_equals_the_facade_limit() -> None:
    from hyprial.daemon.impl.orgfs.document.model import ORGFS_TEXT_MAX as FACADE_LIMIT

    assert ORGFS_TEXT_MAX == FACADE_LIMIT


def test_protected_content_rejection_names_the_path_not_the_doc_id(admission, origin) -> None:
    receiver = origin.clone(90)
    doc_id = _doc_id(origin, DEVICE)
    frame = forge_content(doc_id, origin.state(doc_id), "{}", author=BOB, clock=True, client_id=3)
    result = deliver(admission, receiver, frame)
    _rejected(result, "protected-path")
    assert result.detail["path"] == DEVICE
    assert result.detail["docId"] == doc_id
