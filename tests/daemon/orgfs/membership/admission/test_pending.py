# Decision record
# - 2026-11-25 (F2, design §9 "准入规则的补充"): the tree case of the missing
#   predecessor test stamps the root row; the stamp is now the owner's, since
#   a member rewriting the root row is refused by the root rule.
"""Missing predecessors, staged rebuilds and the clock rule under concurrency."""

from __future__ import annotations

import pytest
from pycrdt import Doc, Map, Text

from hyprial.daemon.impl.orgfs.membership import (
    Accepted,
    Pending,
    Rejected,
    ResultStateAdmission,
    StagedDoc,
    decode_vector,
    encode_vector,
    local_commit,
)
from tests.daemon.orgfs.membership.support import (
    OWNER,
    Frame,
    Replica,
    deliver,
    forge_missing_predecessor,
    forge_text_delete,
)

ALICE = "user:alice"
BOB = "user:bob"


@pytest.fixture
def admission() -> ResultStateAdmission:
    return ResultStateAdmission()


@pytest.fixture
def origin() -> Replica:
    replica = Replica.create(members={ALICE: "rw", BOB: "rw"})
    replica.makedirs("notes", author=BOB)
    replica.write_text("notes/a.md", "hello", author=BOB)
    return replica


def _doc_id(replica: Replica, path: str) -> str:
    return str(replica.tree_value().rows[replica.resolve(path)]["doc_id"])


def _edit_text(text: str, author: str):
    def edit(doc) -> None:
        with local_commit(doc, "content", author):
            body = doc.get("text", type=Text)
            body.clear()
            body.insert(0, text)

    return edit


def _assert_staged_matches(staged: StagedDoc, replica: Replica, doc_id: str) -> None:
    assert decode_vector(staged.vector()) == decode_vector(replica.vector(doc_id))
    assert staged.materialize() == replica.staged(doc_id).materialize()


@pytest.mark.parametrize("doc", ["content", "tree", "meta"])
def test_missing_predecessor_is_pending_and_the_rebuilt_copy_matches(admission, origin, doc) -> None:
    receiver = origin.clone(90)
    if doc == "content":
        doc_id = _doc_id(origin, "notes/a.md")
        first, second = _edit_text("one", ALICE), _edit_text("two", ALICE)
    elif doc == "tree":
        doc_id = "tree"

        def first(d) -> None:
            with local_commit(d, "tree", OWNER, tree_rows=("root",)):
                pass

        second = first
    else:
        doc_id = "meta"

        def first(d) -> None:
            with local_commit(d, "meta", OWNER):
                d.get("members", type=Map)["user:carol"] = Map(
                    {"mode": "ro", "addedBy": OWNER, "addedAt": "t"}
                )

        def second(d) -> None:
            with local_commit(d, "meta", OWNER):
                d.get("members", type=Map)["user:carol"] = Map(
                    {"mode": "rw", "addedBy": OWNER, "addedAt": "t"}
                )

    predecessor, successor = forge_missing_predecessor(
        doc_id, origin.state(doc_id), first, second, client_id=7
    )
    staged = receiver.staged(doc_id)
    result = admission.admit(doc_id, successor.update, successor.vector, staged, receiver.view())
    assert result == Pending("dependency")
    staged.reset(receiver.state(doc_id))
    _assert_staged_matches(staged, receiver, doc_id)
    assert deliver(admission, receiver, predecessor) == Accepted()
    assert deliver(admission, receiver, successor) == Accepted()
    assert decode_vector(receiver.vector(doc_id)) == decode_vector(successor.vector)


def test_pending_never_reaches_the_authoritative_copy(admission, origin) -> None:
    receiver = origin.clone(90)
    doc_id = _doc_id(origin, "notes/a.md")
    _, successor = forge_missing_predecessor(
        doc_id, origin.state(doc_id), _edit_text("one", ALICE), _edit_text("two", ALICE), client_id=7
    )
    before = receiver.state(doc_id)
    assert deliver(admission, receiver, successor) == Pending("dependency")
    assert receiver.state(doc_id) == before


def test_rebuilt_copy_after_rejection_matches_authority(admission, origin) -> None:
    receiver = origin.clone(90)
    bob = origin.clone(3)
    frame = bob.invite("user:mallory", "rw", by=BOB)
    staged = receiver.staged("meta")
    result = admission.admit("meta", frame.update, frame.vector, staged, receiver.view())
    assert isinstance(result, Rejected)
    staged.reset(receiver.state("meta"))
    _assert_staged_matches(staged, receiver, "meta")


def test_malformed_expected_vector_is_rejected(admission, origin) -> None:
    receiver = origin.clone(90)
    frame = origin.invite("user:carol", "ro", by=OWNER)
    result = deliver(admission, receiver, Frame("meta", frame.update, b"\x02\x01"))
    assert isinstance(result, Rejected) and result.code == "invalid-argument"


def test_expected_vector_is_compared_decoded_not_as_bytes(admission, origin) -> None:
    receiver = origin.clone(90)
    frame = origin.invite("user:carol", "ro", by=OWNER)
    reordered = encode_vector(dict(reversed(list(decode_vector(frame.vector).items()))))
    assert deliver(admission, receiver, Frame("meta", frame.update, reordered)) == Accepted()


@pytest.mark.parametrize("alice_first", [True, False])
def test_concurrent_honest_writes_are_accepted_in_either_order(admission, origin, alice_first) -> None:
    alice = origin.clone(2)
    bob = origin.clone(3)
    _, alice_content = alice.write_text("notes/a.md", "alice", author=ALICE)
    _, bob_content = bob.write_text("notes/a.md", "bob", author=BOB)
    receiver = origin.clone(90)
    frames = [alice_content, bob_content] if alice_first else [bob_content, alice_content]
    assert [deliver(admission, receiver, frame) for frame in frames] == [Accepted(), Accepted()]
    value = receiver.staged(alice_content.doc_id).materialize()
    assert value.authors == {ALICE, BOB}
    assert sorted(value.text) == sorted("alicebob")


def _append(doc: Doc, text: str, author: str) -> None:
    with local_commit(doc, "content", author):
        body = doc.get("text", type=Text)
        body.insert(len(body), text)


def _lying_bypass(swap_on_export: bool):
    """Issue 7: a missing-predecessor update with a lying vector, then its predecessor.

    Alice honestly appends to her leave record (P).  Bob's node, holding P,
    appends to the same protected record (M), which admission rejects when
    it can see it.  M is sent first with an empty result vector: its
    structs stay pending, so nothing visible changes and it is accepted.
    """

    origin = Replica.create(members={ALICE: "rw", BOB: "rw"})
    origin.makedirs("directory/leaves", author=OWNER)
    origin.write_text("directory/leaves/alice.json", "v1", author=ALICE)
    doc_id = _doc_id(origin, "directory/leaves/alice.json")
    receiver = origin.clone(90, swap_on_export=swap_on_export)
    sender = Doc(client_id=2)
    sender.apply_update(origin.state(doc_id))
    start = sender.get_state()
    _append(sender, "+alice", ALICE)
    middle = sender.get_state()
    predecessor = Frame(doc_id, sender.get_update(start), middle)
    _append(sender, "+bob", BOB)
    malicious = Frame(doc_id, sender.get_update(middle), b"\x00")
    admission = ResultStateAdmission()
    first = deliver(admission, receiver, malicious)
    unrelated = forge_text_delete(doc_id, origin.state(doc_id), 0, 1, client_id=8)
    assert isinstance(deliver(admission, receiver, unrelated), Rejected)
    second = deliver(admission, receiver, predecessor)
    return first, second, receiver.staged(doc_id).materialize(), receiver.docs[doc_id]


def test_lying_vector_bypass_is_closed_by_export_swap() -> None:
    first, second, value, authority = _lying_bypass(swap_on_export=True)
    assert first == Accepted()
    assert second == Accepted()
    assert value.authors == {ALICE}
    assert value.text == "v1+alice"
    assert authority.get("text", type=Text).to_py() == "v1+alice"
    assert sorted(authority.get("__orgfs_authors__", type=Map).to_py()) == [ALICE]


def test_without_export_swap_the_lying_vector_bypasses_admission() -> None:
    first, second, value, authority = _lying_bypass(swap_on_export=False)
    assert first == Accepted() and second == Accepted()
    assert value.authors == {ALICE, BOB}
    assert authority.get("text", type=Text).to_py() == "v1+alice+bob"
    assert sorted(authority.get("__orgfs_authors__", type=Map).to_py()) == [ALICE, BOB]


def test_honest_vector_on_the_same_update_is_pending() -> None:
    origin = Replica.create(members={ALICE: "rw", BOB: "rw"})
    origin.makedirs("notes", author=OWNER)
    origin.write_text("notes/a.md", "v1", author=ALICE)
    doc_id = _doc_id(origin, "notes/a.md")
    receiver = origin.clone(90)
    sender = Doc(client_id=2)
    sender.apply_update(origin.state(doc_id))
    _append(sender, "+a", ALICE)
    middle = sender.get_state()
    _append(sender, "+b", BOB)
    honest = Frame(doc_id, sender.get_update(middle), sender.get_state())
    assert deliver(ResultStateAdmission(), receiver, honest) == Pending("dependency")
