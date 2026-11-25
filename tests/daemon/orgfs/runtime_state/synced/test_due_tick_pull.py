# Decisions (phase F1 round 2, ruling on F1 item 4, 2026-11-25):
# - After a local write the writer's ``synced`` is false until a current
#   observation covers its meta and tree vectors.  One due anti-entropy tick
#   (``_maintain_spaces``) schedules a pull from an online holder when no
#   observation covers them; once the holder has the write (here by
#   broadcast) that pull makes ``synced`` true, without the holder writing.
# - A tick whose observation already covers the local vectors schedules no
#   pull, and with no holder online nothing is scheduled and ``synced`` stays
#   ``None``.
"""The due tick pulls once when the local vectors are not observed."""

from __future__ import annotations

import time

from ..support import Peers, eventually, text

OWNER = "user:owner@example"
MEMBER = "user:member@example"


def _pair(peers: Peers):
    owner = peers.add("node-owner", OWNER)
    member = peers.add("node-member", MEMBER)
    space_id = owner.fs.create_space("team").space_id
    owner.fs.invite(space_id, MEMBER, "rw")
    member.fs.join(space_id)
    return owner, member, space_id


def _pulls(peer, space_id: str) -> list[str]:
    sync = peer.sync(space_id)
    scheduled: list[str] = []
    original = sync.schedule_sync_from

    def schedule(node: str) -> bool:
        scheduled.append(node)
        return original(node)

    sync.schedule_sync_from = schedule
    return scheduled


def test_one_due_tick_after_a_local_write_makes_synced_true(peers: Peers) -> None:
    owner, member, space_id = _pair(peers)
    member.sync(space_id).sync_from(owner.node_id)
    assert owner.runtime is not None and member.runtime is not None
    member.fs.write_text(space_id, "mine.md", "written by the member")
    eventually(lambda: _equal(text(owner.fs, space_id, "mine.md"), "written by the member"))
    assert member.runtime.status(space_id).synced is False
    scheduled = _pulls(member, space_id)

    member.runtime._maintain_spaces(time.monotonic())  # noqa: SLF001 - one due anti-entropy tick

    assert scheduled == [owner.node_id]
    eventually(lambda: _equal(member.runtime.status(space_id).synced, True))


def test_a_covered_space_schedules_no_pull_and_no_holder_leaves_synced_unknown(peers: Peers) -> None:
    owner, member, space_id = _pair(peers)
    assert member.runtime is not None
    member.sync(space_id).sync_from(owner.node_id)
    eventually(lambda: _equal(member.runtime.status(space_id).synced, True))
    scheduled = _pulls(member, space_id)
    member.runtime._maintain_spaces(time.monotonic())  # noqa: SLF001
    assert scheduled == []

    peers.disconnect(owner)
    member.fs.write_text(space_id, "alone.md", "no holder online")
    member.runtime._maintain_spaces(time.monotonic())  # noqa: SLF001
    assert scheduled == []
    assert member.runtime.status(space_id).synced is None


def _equal(actual: object, expected: object) -> None:
    assert actual == expected
