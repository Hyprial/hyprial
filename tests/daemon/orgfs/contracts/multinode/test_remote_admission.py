# Decisions:
# - Brief item 4 and plan Q2: a write from a member who is read-only or
#   removed *by the receiver's current member table* does not change the
#   receiver's state, whichever order things arrived in.  The only honest way
#   to make such a write reach a peer without forging updates is a partition:
#   the member writes while it still believes it is rw, the owner demotes or
#   removes it meanwhile, then the partition heals.
# - A rejection is observed as "nothing changes for a settle period" after a
#   positive sync point (the member has seen the new member table), using
#   ``holds``.  Forged updates are C2's ``forge_update`` territory.
# - Legacy judges the demoted member's write against the member table the
#   writer had seen (causal meta frontier) and accepts it: legacy_xfail (Q2).
"""Remote-write admission contracts observed through the facade."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError

from ..support import eventually, holds


def _pair(orgfs_impl, tmp_path, mode: str):
    owner = orgfs_impl.make_runtime(tmp_path / "owner", "node-owner", "user:owner")
    member = orgfs_impl.make_runtime(tmp_path / "member", "node-member", "user:member")
    connection = orgfs_impl.connect(owner, member)
    space = owner.facade.create_space("team")
    owner.facade.invite(space.space_id, "user:member", mode)
    member.facade.join(space.space_id)
    owner.facade.write_text(space.space_id, "shared.md", "base")
    eventually(lambda: _assert_text(member.facade, space.space_id, "shared.md", "base"))
    return owner, member, connection, space.space_id


def _assert_text(fs, space_id: str, ref: str, expected: str) -> None:
    assert fs.read_text(space_id, ref)[0] == expected


def _owner_view(fs, space_id: str) -> tuple[object, ...]:
    nodes = tuple((node.node_id, node.path) for node in fs.listdir(space_id, "", recursive=True))
    return nodes, fs.read_text(space_id, "shared.md")[0]


def _assert_member_table(fs, space_id: str, expected: dict[str, str]) -> None:
    assert {row.user: row.mode for row in fs.members(space_id)} == expected


def test_a_read_only_member_cannot_change_the_owner_state(orgfs_impl, tmp_path) -> None:
    owner, member, _connection, space_id = _pair(orgfs_impl, tmp_path, "ro")
    before = _owner_view(owner.facade, space_id)

    with pytest.raises(OrgFsError) as error:
        member.facade.write_text(space_id, "shared.md", "evil")
    owner.facade.write_text(space_id, "marker.md", "sync point")
    eventually(lambda: _assert_text(member.facade, space_id, "marker.md", "sync point"))

    assert error.value.code == "not-a-member"
    holds(lambda: _assert_text(owner.facade, space_id, "shared.md", before[1]))


@pytest.mark.legacy_xfail(
    reason="plan Q2 / design §3.1: writes are judged by the receiver's current member "
    "table; legacy accepts a write the member made before it learned of its demotion"
)
def test_writes_of_a_member_demoted_while_apart_do_not_change_the_owner(
    orgfs_impl, tmp_path
) -> None:
    owner, member, connection, space_id = _pair(orgfs_impl, tmp_path, "rw")
    before = _owner_view(owner.facade, space_id)

    connection.disconnect(member)
    owner.facade.invite(space_id, "user:member", "ro")
    member.facade.write_text(space_id, "shared.md", "written after demotion")
    member.facade.write_text(space_id, "new.md", "created after demotion")
    connection.reconnect(member)
    eventually(
        lambda: _assert_member_table(
            member.facade, space_id, {"user:owner": "rw", "user:member": "ro"}
        )
    )

    holds(lambda: _assert_owner_unchanged(owner.facade, space_id, before))


def test_writes_of_a_member_removed_while_apart_do_not_change_the_owner(
    orgfs_impl, tmp_path
) -> None:
    owner, member, connection, space_id = _pair(orgfs_impl, tmp_path, "rw")
    before = _owner_view(owner.facade, space_id)

    connection.disconnect(member)
    owner.facade.remove_member(space_id, "user:member")
    member.facade.write_text(space_id, "shared.md", "written after removal")
    member.facade.write_text(space_id, "new.md", "created after removal")
    connection.reconnect(member)
    eventually(
        lambda: _assert_member_table(member.facade, space_id, {"user:owner": "rw"})
    )

    holds(lambda: _assert_owner_unchanged(owner.facade, space_id, before))


def _assert_owner_unchanged(fs, space_id: str, before: tuple[object, ...]) -> None:
    assert _owner_view(fs, space_id) == before
