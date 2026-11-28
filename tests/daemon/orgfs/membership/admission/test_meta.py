"""Meta document rows of the §3.6 table (C7 §5.3 items 1 and 3)."""

from __future__ import annotations

import pytest
from pycrdt import Map

from hyprial.daemon.impl.orgfs.membership import (
    Accepted,
    MetaValue,
    Rejected,
    ResultStateAdmission,
    StagedDoc,
    read_member_rows,
)
from tests.daemon.orgfs.membership.support import (
    OWNER,
    Replica,
    StaticView,
    deliver,
    forge_delete_member,
    forge_member_row,
    forge_space_value,
    forge_update,
)

ALICE = "user:alice"
BOB = "user:bob"
READER = "user:reader"


@pytest.fixture
def admission() -> ResultStateAdmission:
    return ResultStateAdmission()


@pytest.fixture
def origin() -> Replica:
    return Replica.create(members={ALICE: "rw", BOB: "rw", READER: "ro"})


def test_owner_invite_and_removal_are_accepted(admission, origin) -> None:
    receiver = origin.clone(90)
    assert deliver(admission, receiver, origin.invite("user:carol", "ro", by=OWNER)) == Accepted()
    assert deliver(admission, receiver, origin.invite("user:carol", "rw", by=OWNER)) == Accepted()
    assert deliver(admission, receiver, origin.remove_member("user:carol", by=OWNER)) == Accepted()
    assert receiver.view().member_mode("user:carol") is None


def test_member_invite_signed_by_member_is_rejected_not_owner(admission, origin) -> None:
    receiver = origin.clone(90)
    bob = origin.clone(3)
    result = deliver(admission, receiver, bob.invite("user:mallory", "rw", by=BOB))
    assert isinstance(result, Rejected) and result.code == "not-owner"
    assert receiver.view().member_mode("user:mallory") is None


def test_read_only_member_cannot_upgrade_own_row(admission, origin) -> None:
    receiver = origin.clone(90)
    reader = origin.clone(4)
    result = deliver(admission, receiver, reader.invite(READER, "rw", by=READER))
    assert isinstance(result, Rejected) and result.code == "not-owner"
    assert receiver.view().member_mode(READER) == "ro"


def test_replacing_an_existing_member_row_is_seen_against_the_baseline(admission, origin) -> None:
    receiver = origin.clone(90)
    frame = forge_member_row(
        origin.state("meta"), ALICE, {"mode": "ro", "addedBy": BOB, "addedAt": "t9"}, client_id=3
    )
    probe = StagedDoc("meta")
    probe.apply(frame.update)
    empty_probe = probe.materialize()
    assert isinstance(empty_probe, MetaValue) and empty_probe.members == {}
    result = deliver(admission, receiver, frame)
    assert isinstance(result, Rejected) and result.code == "not-owner"
    assert result.detail["user"] == ALICE


def test_malformed_member_row_is_rejected(admission, origin) -> None:
    receiver = origin.clone(90)
    frame = forge_member_row(
        origin.state("meta"), "user:x", {"mode": "admin", "addedBy": OWNER, "addedAt": "t"}, client_id=3
    )
    result = deliver(admission, receiver, frame)
    assert isinstance(result, Rejected) and result.code == "not-owner"


def test_owner_change_is_rejected(admission, origin) -> None:
    receiver = origin.clone(90)
    frame = forge_space_value(origin.state("meta"), "owner", BOB, client_id=3)
    result = deliver(admission, receiver, frame)
    assert isinstance(result, Rejected) and result.code == "not-owner"
    assert receiver.view().owner == OWNER


def test_kind_may_be_set_once_and_never_changed(admission, origin) -> None:
    receiver = origin.clone(90)
    assert deliver(admission, receiver, origin.set_space("kind", "home", by=OWNER)) == Accepted()
    result = deliver(admission, receiver, origin.set_space("kind", "work", by=OWNER))
    assert isinstance(result, Rejected) and result.code == "not-owner"


@pytest.mark.parametrize("user", [ALICE, OWNER], ids=["member", "owner"])
def test_no_member_row_key_may_be_deleted(admission, origin, user) -> None:
    receiver = origin.clone(90)
    frame = forge_delete_member(origin.state("meta"), user, client_id=3)
    result = deliver(admission, receiver, frame)
    assert isinstance(result, Rejected) and result.code == "not-owner"
    assert result.detail["reason"] == "member-row-deleted"
    assert receiver.view().member_mode(user) == origin.view().member_mode(user)


def test_owner_marks_a_member_removed_and_readds_them(admission, origin) -> None:
    receiver = origin.clone(90)
    assert deliver(admission, receiver, origin.remove_member(ALICE, by=OWNER)) == Accepted()
    assert receiver.view().member_mode(ALICE) is None
    removed = read_member_rows(receiver.docs["meta"])[ALICE]
    assert removed.removed_at is not None
    assert deliver(admission, receiver, origin.invite(ALICE, "ro", by=OWNER)) == Accepted()
    assert receiver.view().member_mode(ALICE) == "ro"
    readded = read_member_rows(receiver.docs["meta"])[ALICE]
    assert readded.removed_at is None and readded.added_at > removed.added_at


def test_a_removal_mark_by_a_non_owner_is_rejected(admission, origin) -> None:
    receiver = origin.clone(90)
    row = dict(origin.docs["meta"].get("members", type=Map).to_py()[ALICE])
    frame = forge_member_row(
        origin.state("meta"), ALICE, {**row, "addedBy": BOB, "removedAt": "t9"}, client_id=3
    )
    result = deliver(admission, receiver, frame)
    assert isinstance(result, Rejected) and result.code == "not-owner"
    assert receiver.view().member_mode(ALICE) is not None


def test_the_owner_row_cannot_be_marked_removed(admission, origin) -> None:
    receiver = origin.clone(90)
    row = dict(origin.docs["meta"].get("members", type=Map).to_py()[OWNER])
    frame = forge_member_row(origin.state("meta"), OWNER, {**row, "removedAt": "t9"}, client_id=3)
    result = deliver(admission, receiver, frame)
    assert isinstance(result, Rejected) and result.code == "not-owner"
    assert result.detail["reason"] == "owner-row-removed"
    assert receiver.view().member_mode(OWNER) == "rw"


@pytest.mark.parametrize("key", ["spaceId", "createdAt"])
def test_space_id_and_created_at_are_immutable(admission, origin, key) -> None:
    receiver = origin.clone(90)
    owner = origin.clone(5)
    result = deliver(admission, receiver, owner.set_space(key, "changed", by=OWNER))
    assert isinstance(result, Rejected) and result.code == "not-owner"


def test_genesis_meta_from_the_expected_owner_is_accepted(admission, origin) -> None:
    joiner = Replica(client_id=91)
    result = admission.admit(
        "meta", origin.state("meta"), origin.vector("meta"), joiner.staged("meta"),
        StaticView(OWNER, {}),
    )
    assert result == Accepted()


def test_genesis_meta_claiming_another_owner_is_rejected(admission) -> None:
    impostor = Replica.create(owner=BOB, client_id=7)
    joiner = Replica(client_id=91)
    result = admission.admit(
        "meta", impostor.state("meta"), impostor.vector("meta"), joiner.staged("meta"),
        StaticView(OWNER, {}),
    )
    assert isinstance(result, Rejected) and result.code == "not-owner"


@pytest.mark.parametrize("key", ["aclSpaceId", "name", "anything"])
def test_space_root_changes_need_the_owner_as_modified_by(admission, origin, key) -> None:
    receiver = origin.clone(90)
    bob = origin.clone(3)
    result = deliver(admission, receiver, bob.set_space(key, "x", by=BOB))
    assert isinstance(result, Rejected) and result.code == "not-owner"
    assert result.detail["field"] == "space.modifiedBy"
    owner = origin.clone(5)
    assert deliver(admission, receiver, owner.set_space(key, "x", by=OWNER)) == Accepted()


def test_space_root_change_that_clears_modified_by_is_rejected(admission, origin) -> None:
    receiver = origin.clone(90)

    def clear(doc, author) -> None:
        space = doc.get("space", type=Map)
        space["aclSpaceId"] = "x"
        del space["modifiedBy"]

    frame = forge_update("meta", clear, author=BOB, client_id=3, base=origin.state("meta"))
    result = deliver(admission, receiver, frame)
    assert isinstance(result, Rejected) and result.code == "not-owner"
