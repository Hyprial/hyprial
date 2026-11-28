# Decisions (package F8, real-node finding R5-1, rulings 1 and 2, 2026-11-25):
# - A holder that keeps writing while a node joins must not make the join
#   fail after it completed: the join returns, the space is registered with
#   its resident sync, no ``join.pending`` is left, and once the writes stop
#   the space reaches ``synced: true`` within a bounded time (anti-entropy
#   ticks driven by the test, as the daemon's maintenance thread does).
# - The ingress of the temporary sync is slowed (see ``writes.py``) so its
#   queue outlives the close budget, as on the real node.  The temporary
#   sync's close must finish within its budget (queued broadcasts are
#   discarded, not waited for), so no ``temporary-close-failed`` is logged.
"""Join while the holder keeps writing."""

from __future__ import annotations

import time

from ..support import Peers, eventually
from .writes import Writer, assert_joined, slow_ingress, slow_join_round

OWNER = "user:owner@example"
MEMBER = "user:member@example"


def _synced(peer, space_id: str) -> None:
    assert peer.runtime is not None
    peer.runtime.anti_entropy_tick(time.monotonic())
    status = peer.runtime.status(space_id)
    assert (status.synced, status.content_pending) == (True, 0)


def test_a_join_while_the_holder_keeps_writing_succeeds_and_catches_up(peers: Peers) -> None:
    owner = peers.add("node-owner", OWNER)
    member = peers.add("node-member", MEMBER)
    space_id = owner.fs.create_space("team").space_id
    owner.fs.invite(space_id, MEMBER, "rw")
    owner.fs.write_text(space_id, "base.md", "base")
    slow_ingress(member, 0.06)
    slow_join_round(member, 2.0)

    with Writer(owner, space_id) as writer:
        info = member.fs.join(space_id)
        assert_joined(member, space_id)
        written = writer.stop()

    assert info.space_id == space_id
    assert written > 0
    assert "orgfs.join.temporary-close-failed" not in member.events.names()
    eventually(lambda: _synced(member, space_id), timeout=60.0)
    assert_joined(member, space_id)
