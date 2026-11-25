# Decisions (phase F1, item 2, design §9.2, 2026-11-25):
# - A member removed while partitioned learns its removal from the meta-only
#   answer a supplier gives a requester whose member row carries
#   ``removedAt``; it never receives tree, content or blob updates made after
#   the removal, its local writes then report ``not-a-member``, and its pushes
#   stay refused by the owner's node gate.
# - Nodes run the production configuration (trust off); device owners come
#   from a fixed ``node -> author`` map, as the contract factory provides them,
#   so the test isolates the supplier's answer from the directory bootstrap.
# - A node whose author never was a member is still refused (join answers
#   ``not-a-member``).
"""A removed member learns its removal and nothing else."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError
from hyprial.daemon.impl.orgfs.composition.state import StateOrgFsRuntime

from ..support import Peers, eventually, holds, text

OWNERS = {
    "node-owner": "user:owner@example",
    "node-member": "user:member@example",
    "node-stranger": "user:stranger@example",
}


class _FixedOwnerRuntime(StateOrgFsRuntime):
    def _device_owner_resolver(self, space_id: str) -> Any:
        return OWNERS.get


def _names(peer, space_id: str) -> set[str]:
    return {node.name for node in peer.fs.listdir(space_id, "", recursive=True)}


def _members(peer, space_id: str) -> set[str]:
    return {row.user for row in peer.fs.members(space_id)}


def test_a_removed_member_learns_its_removal_and_nothing_after_it(peers: Peers) -> None:
    owner = peers.add("node-owner", OWNERS["node-owner"], trust=False, runtime_class=_FixedOwnerRuntime)
    member = peers.add("node-member", OWNERS["node-member"], trust=False, runtime_class=_FixedOwnerRuntime)
    space_id = owner.fs.create_space("team").space_id
    owner.fs.invite(space_id, member.author, "rw")
    member.fs.join(space_id)
    owner.fs.write_text(space_id, "shared.md", "base")
    eventually(lambda: _assert(text(member.fs, space_id, "shared.md") == "base"))

    peers.disconnect(member)
    owner.fs.remove_member(space_id, member.author)
    owner.fs.write_text(space_id, "shared.md", "after removal")
    owner.fs.write_text(space_id, "after.md", "secret")
    payload = b"\x00" + b"B" * 300_000
    owner.fs.write_bytes(space_id, "after.bin", payload)
    member.fs.write_text(space_id, "mine.md", "written while apart")
    peers.reconnect(member)
    peers.pull_all(space_id)

    eventually(lambda: _assert(_members(member, space_id) == {owner.author}))
    with pytest.raises(OrgFsError) as raised:
        member.fs.write_text(space_id, "late.md", "after learning")
    assert raised.value.code == "not-a-member"

    def nothing_after_removal() -> None:
        peers.pull_all(space_id)
        assert "after.md" not in _names(member, space_id)
        assert "after.bin" not in _names(member, space_id)
        assert text(member.fs, space_id, "shared.md") == "base"
        assert "mine.md" not in _names(owner, space_id)
        assert "late.md" not in _names(owner, space_id)

    holds(nothing_after_removal, duration=1.0)
    assert member.runtime is not None
    assert not member.runtime.blobs.has(hashlib.sha256(payload).hexdigest())


def test_a_node_whose_author_never_was_a_member_is_refused(peers: Peers) -> None:
    owner = peers.add("node-owner", OWNERS["node-owner"], trust=False, runtime_class=_FixedOwnerRuntime)
    stranger = peers.add("node-stranger", OWNERS["node-stranger"], trust=False, runtime_class=_FixedOwnerRuntime)
    space_id = owner.fs.create_space("team").space_id
    owner.fs.write_text(space_id, "shared.md", "base")

    with pytest.raises(OrgFsError) as raised:
        stranger.fs.join(space_id)
    assert raised.value.code == "not-a-member"
    assert space_id not in {info.space_id for info in stranger.fs.spaces()}


def _assert(condition: bool) -> None:
    assert condition
