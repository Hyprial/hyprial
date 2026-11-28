"""Member table helpers and the local write check (design §3.2, §3.4, §6, §9 R8)."""

from __future__ import annotations

import pytest
from pycrdt import Doc, Map

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.membership import (
    LocalWrite,
    MemberRow,
    ProtectionPolicy,
    check_local_write,
    local_write_error,
    mark_member_removed,
    member_mode,
    parse_member_row,
    put_member,
    read_member_rows,
    read_members,
    read_space,
    readded_at,
    write_space,
)
from hyprial.identity import protected_directory_doc_id, protected_directory_node_id
from tests.daemon.orgfs.membership.support import ORG_NAME, OWNER, SPACE_ID, StaticView

ALICE = "user:alice"
BOB = "user:bob"
READER = "user:reader"
ALICE_DEVICE = "directory/devices/alice/laptop.json"


def _view() -> StaticView:
    return StaticView(OWNER, {OWNER: "rw", ALICE: "rw", BOB: "rw", READER: "ro"})


def _policy(name: str = ORG_NAME) -> ProtectionPolicy:
    return ProtectionPolicy(space_id=SPACE_ID, space_name=name, owner=OWNER)


def test_space_and_member_rows_round_trip() -> None:
    doc = Doc(client_id=31)
    with doc.transaction():
        write_space(doc, space_id=SPACE_ID, name=ORG_NAME, owner=OWNER, created_at="t0")
        put_member(doc, OWNER, mode="rw", added_by=OWNER, added_at="t0")
        put_member(doc, ALICE, mode="ro", added_by=OWNER, added_at="t1")
    assert read_space(doc) == {
        "spaceId": SPACE_ID, "name": ORG_NAME, "owner": OWNER, "createdAt": "t0",
    }
    assert read_members(doc)[ALICE] == MemberRow("ro", OWNER, "t1")
    with doc.transaction():
        put_member(doc, ALICE, mode="rw", added_by=OWNER, added_at="t1")
    assert read_members(doc)[ALICE].mode == "rw"
    with doc.transaction():
        mark_member_removed(doc, ALICE, removed_at="t2")
    assert set(read_members(doc)) == {OWNER}
    assert read_member_rows(doc)[ALICE] == MemberRow("rw", OWNER, "t1", "t2")


T0 = "2026-11-25T08:00:00.000000Z"
T1 = "2026-11-25T09:00:00.000000Z"
T2 = "2026-11-25T10:00:00.000000Z"


def _members_doc() -> Doc:
    doc = Doc(client_id=32)
    put_member(doc, OWNER, mode="rw", added_by=OWNER, added_at=T0)
    put_member(doc, ALICE, mode="ro", added_by=OWNER, added_at=T1)
    return doc


def test_removal_keeps_the_row_with_removed_at() -> None:
    doc = _members_doc()
    row = mark_member_removed(doc, ALICE, removed_at=T2)
    assert row == MemberRow("ro", OWNER, T1, T2)
    assert doc.get("members", type=Map).to_py()[ALICE] == {
        "mode": "ro", "addedBy": OWNER, "addedAt": T1, "removedAt": T2,
    }
    assert ALICE not in read_members(doc)
    assert member_mode(doc, ALICE) is None
    assert member_mode(doc, OWNER) == "rw"
    assert read_member_rows(doc)[ALICE].is_member is False


def test_removing_a_non_member_raises_key_error() -> None:
    doc = _members_doc()
    mark_member_removed(doc, ALICE, removed_at=T2)
    with pytest.raises(KeyError):
        mark_member_removed(doc, ALICE, removed_at=T2)
    with pytest.raises(KeyError):
        mark_member_removed(doc, "user:nobody", removed_at=T2)


def test_mode_change_of_a_current_member_keeps_added_at() -> None:
    doc = _members_doc()
    row = put_member(doc, ALICE, mode="rw", added_by=OWNER, added_at=T2)
    assert row == MemberRow("rw", OWNER, T1)
    assert read_members(doc)[ALICE] == MemberRow("rw", OWNER, T1)


def test_readding_clears_removed_at_and_takes_a_later_added_at() -> None:
    doc = _members_doc()
    mark_member_removed(doc, ALICE, removed_at=T2)
    row = put_member(doc, ALICE, mode="rw", added_by=OWNER, added_at=T2)
    assert row == MemberRow("rw", OWNER, T2)
    assert "removedAt" not in doc.get("members", type=Map).to_py()[ALICE]
    assert member_mode(doc, ALICE) == "rw"


@pytest.mark.parametrize("proposed", [T1, T0, "not-a-time"], ids=["same", "earlier", "garbage"])
def test_readding_with_a_clock_not_later_takes_old_plus_one_microsecond(proposed: str) -> None:
    doc = _members_doc()
    mark_member_removed(doc, ALICE, removed_at=T2)
    row = put_member(doc, ALICE, mode="ro", added_by=OWNER, added_at=proposed)
    assert row.added_at == "2026-11-25T09:00:00.000001Z"
    assert read_members(doc)[ALICE].added_at == "2026-11-25T09:00:00.000001Z"


def test_readded_at_orders_timestamps_as_datetimes() -> None:
    assert readded_at("2026-11-25T09:00:00Z", "2026-11-25T09:00:00.000000Z") == (
        "2026-11-25T09:00:00.000001Z"
    )
    assert readded_at("2026-11-25T09:00:00+08:00", "2026-11-25T02:00:00.5Z") == (
        "2026-11-25T02:00:00.5Z"
    )
    assert readded_at("old-format", "2026-11-25T09:00:00Z") == "2026-11-25T09:00:00Z"
    with pytest.raises(ValueError):
        readded_at("old-format", "old-format")


def test_write_space_records_kind_only_when_given() -> None:
    doc = Doc()
    write_space(doc, space_id=SPACE_ID, name="home", owner=OWNER, created_at="t0", kind="home")
    assert read_space(doc)["kind"] == "home"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "rw",
        {"mode": "admin", "addedBy": OWNER, "addedAt": "t"},
        {"mode": "rw", "addedBy": "", "addedAt": "t"},
        {"mode": "rw", "addedBy": OWNER},
        {"mode": "rw", "addedBy": OWNER, "addedAt": 3},
        {"mode": "rw", "addedBy": OWNER, "addedAt": "t", "other": "t"},
        {"mode": "rw", "addedBy": OWNER, "addedAt": "t", "removedAt": None},
        {"mode": "rw", "addedBy": OWNER, "addedAt": "t", "removedAt": ""},
        {"mode": "rw", "addedBy": OWNER, "addedAt": "t", "removedAt": 3},
    ],
)
def test_malformed_member_rows_do_not_parse(value: object) -> None:
    assert parse_member_row(value) is None


def test_member_row_may_carry_a_reserved_signature_slot() -> None:
    row = parse_member_row({"mode": "rw", "addedBy": OWNER, "addedAt": "t", "sig": {}})
    assert row == MemberRow("rw", OWNER, "t")


def test_member_row_with_removed_at_parses_as_not_a_member() -> None:
    row = parse_member_row({"mode": "rw", "addedBy": OWNER, "addedAt": "t", "removedAt": "u"})
    assert row == MemberRow("rw", OWNER, "t", "u")
    assert row is not None and row.is_member is False


def test_member_helpers_reject_unknown_mode() -> None:
    with pytest.raises(ValueError):
        put_member(Doc(), ALICE, mode="admin", added_by=OWNER, added_at="t")


def test_rw_member_may_write_ordinary_paths() -> None:
    assert local_write_error(ALICE, LocalWrite("tree", paths=("notes/a.md",)), _view(), _policy()) is None
    assert local_write_error(ALICE, LocalWrite("content", paths=("notes/a.md",), doc_id="doc-1"), _view(), _policy()) is None


@pytest.mark.parametrize("caller", [READER, "user:stranger"])
def test_read_only_and_non_members_are_refused_not_a_member(caller: str) -> None:
    with pytest.raises(OrgFsError) as error:
        check_local_write(caller, LocalWrite("tree", paths=("notes/a.md",)), _view(), _policy())
    assert error.value.code == "not-a-member"


@pytest.mark.parametrize("caller", [ALICE, READER])
def test_only_owner_writes_meta(caller: str) -> None:
    assert local_write_error(OWNER, LocalWrite("meta"), _view(), _policy()) is None
    error = local_write_error(caller, LocalWrite("meta"), _view(), _policy())
    assert error is not None and error.code == "not-owner"


def test_protected_path_needs_the_path_author() -> None:
    write = LocalWrite("content", paths=(ALICE_DEVICE,))
    assert local_write_error(ALICE, write, _view(), _policy()) is None
    error = local_write_error(BOB, write, _view(), _policy())
    assert error is not None and error.code == "protected-path"
    owner_error = local_write_error(OWNER, write, _view(), _policy())
    assert owner_error is not None and owner_error.code == "protected-path"


def test_protected_doc_and_node_ids_are_checked_without_a_path() -> None:
    doc_id = protected_directory_doc_id(SPACE_ID, ALICE_DEVICE, space_owner=OWNER)
    node_id = protected_directory_node_id(SPACE_ID, ALICE_DEVICE, space_owner=OWNER)
    assert doc_id is not None and node_id is not None
    for write in (LocalWrite("content", doc_id=doc_id), LocalWrite("tree", node_ids=(node_id,))):
        assert local_write_error(ALICE, write, _view(), _policy()) is None
        error = local_write_error(BOB, write, _view(), _policy())
        assert error is not None and error.code == "protected-path"


def test_owner_only_sections_and_user_keys() -> None:
    invite = LocalWrite("content", paths=("directory/invites/x.json",))
    assert local_write_error(OWNER, invite, _view(), _policy()) is None
    error = local_write_error(ALICE, invite, _view(), _policy())
    assert error is not None and error.code == "protected-path"
    keys = LocalWrite("content", paths=("directory/user-keys/alice/k.json",))
    error = local_write_error(ALICE, keys, _view(), _policy())
    assert error is not None and error.code == "protected-path"


def test_protection_applies_only_in_org_directory_spaces() -> None:
    write = LocalWrite("content", paths=(ALICE_DEVICE,))
    assert local_write_error(BOB, write, _view(), _policy("home")) is None


def test_member_check_comes_before_path_check() -> None:
    error = local_write_error(READER, LocalWrite("content", paths=(ALICE_DEVICE,)), _view(), _policy())
    assert error is not None and error.code == "not-a-member"


def test_permanent_remove_of_a_protected_path_is_refused_even_by_alias() -> None:
    node_id = protected_directory_node_id(SPACE_ID, ALICE_DEVICE, space_owner=OWNER)
    assert node_id is not None
    by_alias = LocalWrite("tree", node_ids=(node_id,), paths=("anything",), permanent=True, node_author=ALICE)
    error = local_write_error(OWNER, by_alias, _view(), _policy())
    assert error is not None and error.code == "protected-path"
    by_path = LocalWrite("tree", paths=(ALICE_DEVICE,), permanent=True, node_author=ALICE)
    error = local_write_error(BOB, by_path, _view(), _policy())
    assert error is not None and error.code == "protected-path"


def test_permanent_remove_needs_owner_or_node_author() -> None:
    write = LocalWrite("tree", paths=("notes/a.md",), permanent=True, node_author=ALICE)
    assert local_write_error(ALICE, write, _view(), _policy()) is None
    assert local_write_error(OWNER, write, _view(), _policy()) is None
    error = local_write_error(BOB, write, _view(), _policy())
    assert error is not None and error.code == "not-owner"


def test_check_local_write_raises_with_details() -> None:
    with pytest.raises(OrgFsError) as error:
        check_local_write(BOB, LocalWrite("content", paths=(ALICE_DEVICE,)), _view(), _policy())
    assert error.value.code == "protected-path"
    assert error.value.details["path"] == ALICE_DEVICE
    check_local_write(ALICE, LocalWrite("content", paths=(ALICE_DEVICE,)), _view(), _policy())


def test_protection_policy_rules() -> None:
    policy = _policy()
    rule = policy.rule_for_path(ALICE_DEVICE)
    assert rule is not None and rule.section == "devices" and rule.principal == ALICE
    assert policy.allows(rule, ALICE) and not policy.allows(rule, BOB)
    assert policy.rule_for_path("notes/a.md") is None
    assert policy.rule_for_path("") is None
    owner_dir = policy.rule_for_path("directory")
    assert owner_dir is not None and policy.allows(owner_dir, OWNER)
    assert not policy.allows(owner_dir, ALICE)
    foreign = protected_directory_doc_id("other-space", ALICE_DEVICE, space_owner=OWNER)
    assert foreign is not None
    foreign_rule = policy.rule_for_doc(foreign)
    assert foreign_rule is not None and not policy.allows(foreign_rule, ALICE)
    broken = policy.rule_for_doc("doc-orgdir-v3-!!!")
    assert broken is not None and not policy.allows(broken, OWNER)
    assert policy.rule_for_doc("doc-00000000-0000-0000-0000-000000000001") is None
