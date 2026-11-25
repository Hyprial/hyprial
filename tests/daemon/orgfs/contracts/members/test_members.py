# Decisions:
# - Migrated from contracts/test_orgfs_api.py: non-owner / join-needs-holder,
#   joined non-owner gates (``FakeJoinMesh`` replaced by a real join over
#   ``orgfs_impl.connect``), core metadata immutability and re-invite
#   ``added_at``.  The original monkeypatched the clock; this version only
#   compares ``added_at`` values the facade returns.
# - F1 (T3 report §6-2): a member invited again after removal gets an
#   ``added_at`` strictly later than the removed one (design §9 R8).
# - Design §3.2 / §3.4: only the owner changes members and the space root;
#   a ro member's local write is refused with ``not-a-member``.
"""Membership contracts: invite, members, remove_member, meta, join."""

from __future__ import annotations

from datetime import datetime

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError

from ..support import eventually


def test_owner_is_the_first_member_and_invite_adds_with_mode(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")

    added = fs.invite(space.space_id, "user:reader", "ro")

    assert (added.user, added.mode, added.added_by) == ("user:reader", "ro", "user:owner")
    members = {member.user: member for member in fs.members(space.space_id)}
    assert set(members) == {"user:owner", "user:reader"}
    assert members["user:owner"].mode == "rw"
    assert members["user:owner"].added_at == space.created_at
    assert members["user:reader"].mode == "ro"


def test_reinvite_changes_mode_but_keeps_added_at(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    first = fs.invite(space.space_id, "user:reader", "ro")

    again = fs.invite(space.space_id, "user:reader", "rw")
    fs.invite(space.space_id, "user:owner", "rw")

    assert again.mode == "rw"
    assert again.added_at == first.added_at
    members = {member.user: member for member in fs.members(space.space_id)}
    assert members["user:reader"].added_at == first.added_at
    assert members["user:owner"].added_at == space.created_at


def test_remove_member_then_reinvite_is_a_new_member(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    fs.invite(space.space_id, "user:reader", "rw")

    fs.remove_member(space.space_id, "user:reader")
    assert {member.user for member in fs.members(space.space_id)} == {"user:owner"}

    back = fs.invite(space.space_id, "user:reader", "rw")
    assert {member.user for member in fs.members(space.space_id)} == {"user:owner", "user:reader"}
    assert back.added_by == "user:owner"


def test_a_member_invited_again_after_removal_gets_a_later_added_at(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    first = fs.invite(space.space_id, "user:reader", "rw")

    fs.remove_member(space.space_id, "user:reader")
    back = fs.invite(space.space_id, "user:reader", "rw")

    listed = {member.user: member for member in fs.members(space.space_id)}["user:reader"]
    assert listed.added_at == back.added_at
    assert _instant(back.added_at) > _instant(first.added_at)


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_member_errors_without_the_space(orgfs_impl) -> None:
    owner = orgfs_impl.make_facade("user:owner")
    space = owner.create_space("team")
    stranger = orgfs_impl.make_facade("user:reader")

    with pytest.raises(OrgFsError) as missing:
        owner.remove_member(space.space_id, "user:missing")
    assert missing.value.code == "invalid-argument"
    with pytest.raises(OrgFsError) as unknown:
        stranger.invite(space.space_id, "user:other")
    assert unknown.value.code == "unknown-space"
    with pytest.raises(OrgFsError) as no_holder:
        stranger.join(space.space_id)
    assert no_holder.value.code == "no-holder-online"


def test_update_space_meta_merges_custom_keys(orgfs_impl) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")

    updated = fs.update_space_meta(space.space_id, {"aclSpaceId": "acl-1"})

    assert updated["aclSpaceId"] == "acl-1"
    meta = fs.space_meta(space.space_id)
    assert meta["aclSpaceId"] == "acl-1"
    assert (meta["owner"], meta["name"], meta["spaceId"]) == (
        "user:owner",
        "team",
        space.space_id,
    )


@pytest.mark.parametrize("key", ("owner", "name", "spaceId", "createdAt"))
def test_core_space_metadata_is_immutable(orgfs_impl, key: str) -> None:
    fs = orgfs_impl.make_facade("user:owner")
    space = fs.create_space("team")
    before = fs.space_meta(space.space_id)

    with pytest.raises(OrgFsError) as error:
        fs.update_space_meta(space.space_id, {key: "attacker-value"})

    assert error.value.code == "invalid-argument"
    assert fs.space_meta(space.space_id) == before


def _joined(orgfs_impl, tmp_path, mode: str):
    owner = orgfs_impl.make_runtime(tmp_path / "owner", "node-owner", "user:owner")
    member = orgfs_impl.make_runtime(tmp_path / "member", "node-member", "user:member")
    connection = orgfs_impl.connect(owner, member)
    space = owner.facade.create_space("team")
    owner.facade.invite(space.space_id, "user:member", mode)
    assert member.facade.join(space.space_id) == space
    return owner, member, connection, space


def test_joined_non_owner_cannot_change_members_or_meta(orgfs_impl, tmp_path) -> None:
    owner, member, _connection, space = _joined(orgfs_impl, tmp_path, "rw")
    assert {row.user for row in member.facade.members(space.space_id)} >= {
        "user:owner",
        "user:member",
    }

    for action in (
        lambda: member.facade.invite(space.space_id, "user:other"),
        lambda: member.facade.remove_member(space.space_id, "user:owner"),
        lambda: member.facade.update_space_meta(space.space_id, {"aclSpaceId": "attacker"}),
    ):
        with pytest.raises(OrgFsError) as error:
            action()
        assert error.value.code == "not-owner"
    assert "aclSpaceId" not in owner.facade.space_meta(space.space_id)
    assert {row.user for row in owner.facade.members(space.space_id)} == {
        "user:owner",
        "user:member",
    }


def test_owner_membership_changes_reach_a_connected_member(orgfs_impl, tmp_path) -> None:
    owner, member, _connection, space = _joined(orgfs_impl, tmp_path, "rw")

    owner.facade.invite(space.space_id, "user:carol", "ro")

    eventually(lambda: _assert_mode(member.facade, space.space_id, "user:carol", "ro"))


@pytest.mark.legacy_xfail(
    reason="design §2.5 step 3 / §4.4: every local commit is published after it is "
    "durable; legacy update_space_meta is not broadcast and reaches peers only with a "
    "later tree or content write"
)
def test_owner_meta_update_reaches_a_connected_member(orgfs_impl, tmp_path) -> None:
    owner, member, _connection, space = _joined(orgfs_impl, tmp_path, "rw")

    owner.facade.update_space_meta(space.space_id, {"aclSpaceId": "acl-1"})

    eventually(lambda: _assert_meta(member.facade, space.space_id, "aclSpaceId", "acl-1"))


@pytest.mark.parametrize(
    "write",
    [
        lambda fs, sid: fs.write_text(sid, "shared.md", "evil"),
        lambda fs, sid: fs.write_text(sid, "new.md", "evil"),
        lambda fs, sid: fs.write_bytes(sid, "new.bin", b"evil"),
        lambda fs, sid: fs.mkdir(sid, "evil"),
        lambda fs, sid: fs.move(sid, "shared.md", "moved.md"),
        lambda fs, sid: fs.remove(sid, "shared.md"),
    ],
    ids=["write-existing", "write-new", "write-bytes", "mkdir", "move", "remove"],
)
def test_read_only_member_local_writes_are_refused(orgfs_impl, tmp_path, write) -> None:
    owner, member, _connection, space = _joined(orgfs_impl, tmp_path, "ro")
    owner.facade.write_text(space.space_id, "shared.md", "base")
    eventually(lambda: _assert_text(member.facade, space.space_id, "shared.md", "base"))
    before = member.facade.listdir(space.space_id, "", recursive=True)

    with pytest.raises(OrgFsError) as error:
        write(member.facade, space.space_id)

    assert error.value.code == "not-a-member"
    assert member.facade.listdir(space.space_id, "", recursive=True) == before
    assert member.facade.read_text(space.space_id, "shared.md")[0] == "base"


def test_promoted_member_can_write_and_the_owner_sees_it(orgfs_impl, tmp_path) -> None:
    owner, member, _connection, space = _joined(orgfs_impl, tmp_path, "ro")

    owner.facade.invite(space.space_id, "user:member", "rw")
    eventually(lambda: _assert_mode(member.facade, space.space_id, "user:member", "rw"))
    created = member.facade.write_text(space.space_id, "from-member.md", "hello")

    eventually(lambda: _assert_text(owner.facade, space.space_id, f"id:{created.node_id}", "hello"))
    assert owner.facade.stat(space.space_id, f"id:{created.node_id}").modified_by == "user:member"


def test_removed_member_local_writes_are_refused(orgfs_impl, tmp_path) -> None:
    owner, member, _connection, space = _joined(orgfs_impl, tmp_path, "rw")
    owner.facade.remove_member(space.space_id, "user:member")
    eventually(
        lambda: _assert_absent(member.facade, space.space_id, "user:member")
    )

    with pytest.raises(OrgFsError) as error:
        member.facade.write_text(space.space_id, "late.md", "late")

    assert error.value.code == "not-a-member"


def _assert_text(fs, space_id: str, ref: str, expected: str) -> None:
    assert fs.read_text(space_id, ref)[0] == expected


def _assert_meta(fs, space_id: str, key: str, expected: object) -> None:
    assert fs.space_meta(space_id).get(key) == expected


def _assert_mode(fs, space_id: str, user: str, mode: str) -> None:
    assert {row.user: row.mode for row in fs.members(space_id)}.get(user) == mode


def _assert_absent(fs, space_id: str, user: str) -> None:
    assert user not in {row.user for row in fs.members(space_id)}
