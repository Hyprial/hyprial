# Decisions:
# - Design §9 R5: a join fills a temporary v3 space from a holder and keeps it
#   only when the meta names an owner, this node is a member and the holder
#   passes the node gate.  The three failures keep the legacy join's codes
#   (measured on ``OrgFsRuntime`` before D2): no candidate holder is
#   ``no-holder-online``; an uninvited requester is refused ``not-a-member``;
#   a holder that is offline when the pull ends is ``not-a-member`` with
#   ``reason: supplier-not-online`` (the retryable shape of
#   ``OrgNetworkService._join_with_retry``).  Unlike legacy, none leaves a
#   directory on disk, and a later join of the same space succeeds.
"""Join of ``StateOrgFsRuntime``: success and the three failures."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError

from .support import Peer, Peers, eventually, text, tree


def _space_dirs(peer: Peer) -> list[str]:
    root = peer.spaces_root
    return sorted(path.name for path in root.iterdir()) if root.exists() else []


def _assert_not_joined(peer: Peer, space_id: str) -> None:
    assert peer.fs.spaces() == ()
    with pytest.raises(OrgFsError) as error:
        peer.fs.stat(space_id, "id:root")
    assert error.value.code == "unknown-space"
    assert _space_dirs(peer) == []


def test_join_copies_the_space_and_announces_the_new_holder(peers: Peers) -> None:
    owner = peers.add("node-owner", "user:owner")
    member = peers.add("node-member", "user:member")
    space = owner.fs.create_space("team")
    owner.fs.write_text(space.space_id, "a.md", "alpha")
    owner.fs.write_bytes(space.space_id, "b.bin", b"\x00bytes")
    owner.fs.invite(space.space_id, "user:member", "ro")

    assert member.fs.join(space.space_id) == space

    assert member.fs.spaces() == (space,)
    assert member.fs.members(space.space_id) == owner.fs.members(space.space_id)
    eventually(lambda: _same_tree(owner, member, space.space_id))
    eventually(lambda: _assert_text(member, space.space_id, "a.md", "alpha"))
    assert _space_dirs(member) == [space.space_id]
    assert owner.runtime is not None
    eventually(lambda: _assert_holder(owner, space.space_id, "node-member"))
    assert member.fs.join(space.space_id) == space


def test_join_without_any_holder_is_no_holder_online_and_leaves_no_directory(peers: Peers) -> None:
    lonely = peers.add("node-lonely", "user:lonely")
    space_id = "6f1a4d3e-0000-4000-8000-000000000001"

    with pytest.raises(OrgFsError) as error:
        lonely.fs.join(space_id)

    assert error.value.code == "no-holder-online"
    _assert_not_joined(lonely, space_id)


def test_join_by_an_uninvited_node_is_not_a_member_and_leaves_no_directory(peers: Peers) -> None:
    owner = peers.add("node-owner", "user:owner")
    stranger = peers.add("node-stranger", "user:stranger")
    space = owner.fs.create_space("team")
    owner.fs.write_text(space.space_id, "secret.md", "not for strangers")

    with pytest.raises(OrgFsError) as error:
        stranger.fs.join(space.space_id)

    assert error.value.code == "not-a-member"
    _assert_not_joined(stranger, space.space_id)


def test_join_from_an_offline_holder_is_retryable_and_leaves_no_directory(peers: Peers) -> None:
    owner = peers.add("node-owner", "user:owner")
    member = peers.add("node-member", "user:member")
    space = owner.fs.create_space("team")
    owner.fs.invite(space.space_id, "user:member", "rw")
    peers.disconnect(owner)

    with pytest.raises(OrgFsError) as error:
        member.fs.join(space.space_id)

    assert error.value.code == "not-a-member"
    assert error.value.details["reason"] == "supplier-not-online"
    _assert_not_joined(member, space.space_id)


def test_a_holder_lost_in_the_middle_of_the_join_leaves_no_directory(peers: Peers) -> None:
    owner = peers.add("node-owner", "user:owner")
    member = peers.add("node-member", "user:member")
    space = owner.fs.create_space("team")
    for index in range(3):
        owner.fs.write_text(space.space_id, f"file-{index}.md", f"body-{index}")
    owner.fs.invite(space.space_id, "user:member", "rw")
    original = member.fs.apply_remote
    calls: list[str] = []

    def apply_then_lose_the_holder(space_id, doc_id, update, vector, peer):
        outcome = original(space_id, doc_id, update, vector, peer)
        calls.append(doc_id)
        if len(calls) == 1:
            peers.disconnect(owner)
        return outcome

    member.fs.apply_remote = apply_then_lose_the_holder
    try:
        with pytest.raises(OrgFsError) as error:
            member.fs.join(space.space_id)
    finally:
        del member.fs.apply_remote

    assert calls and calls[0] == "meta"
    assert error.value.code == "not-a-member"
    assert error.value.details["reason"] == "supplier-not-online"
    _assert_not_joined(member, space.space_id)

    peers.reconnect(owner)
    assert member.fs.join(space.space_id) == space
    eventually(lambda: _assert_text(member, space.space_id, "file-2.md", "body-2"))


def _same_tree(left: Peer, right: Peer, space_id: str) -> None:
    assert tree(left.fs, space_id) == tree(right.fs, space_id)


def _assert_text(peer: Peer, space_id: str, ref: str, expected: str) -> None:
    assert text(peer.fs, space_id, ref) == expected


def _assert_holder(peer: Peer, space_id: str, holder: str) -> None:
    assert peer.runtime is not None
    assert holder in peer.runtime.status(space_id).holders_online


def test_join_of_a_space_id_that_is_not_a_uuid_is_unknown_space(peers: Peers) -> None:
    owner = peers.add("node-owner", "user:owner")
    member = peers.add("node-member", "user:member")
    owner.fs.create_space("team")

    with pytest.raises(OrgFsError) as error:
        member.fs.join("space-late")

    assert error.value.code == "unknown-space"
    _assert_not_joined(member, "space-late")
