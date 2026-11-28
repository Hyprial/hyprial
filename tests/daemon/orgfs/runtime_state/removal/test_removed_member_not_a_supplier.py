# Decisions (package F7, real-node finding R4-3, rulings 3 and 4, 2026-11-25):
# - In the fourth real-node round the owner pulled from a removed member's
#   node about every 60 s; the failures reached the shared supplier backoff
#   (9 failures, 120 s hold) and held back that node's other spaces, and the
#   owner's ``synced`` stayed false because of the removed member while the
#   remaining member was online.
# - The node gate therefore runs before a supplier is chosen: a removed
#   member's node is never scheduled (online edge, due tick, explicit
#   request), leaves no failure record and no shared backoff, and its
#   observation is ignored by ``synced``.  Once re-invited it is a candidate
#   again.
# - The removed member's ahead observation is planted directly in the
#   owner's sync: the in-process run cannot reproduce how the real owner came
#   to hold it, and the rule under test is only which observations count.
"""A removed member's node is no supplier and does not decide ``synced``."""

from __future__ import annotations

import time

from hyprial.daemon.impl.orgfs.sync.interface import PeerObservation

from ..convergence.team import TICK_SECONDS, members, settle, synced, team, tick
from ..support import Peers, eventually


def _check(condition: bool) -> None:
    assert condition


def test_a_removed_member_node_is_never_scheduled_and_leaves_no_failure(peers: Peers) -> None:
    alice, bob, carol, space_id = team(peers)
    group = (alice, bob, carol)
    alice.fs.remove_member(space_id, bob.author)
    eventually(lambda: _check(bob.author not in members(carol, space_id)))
    settle(group, space_id)
    sync = alice.sync(space_id)
    assert alice.runtime is not None
    queried: list[str] = []
    session = sync.session
    original = session.query

    def query(key_expr: str, payload: bytes, **kwargs):
        if "/org/fs3/sync/" in key_expr:
            queried.append(key_expr.rsplit("/", 1)[-1])
        return original(key_expr, payload, **kwargs)

    session.query = query
    before = len(alice.events.seen)

    assert sync.schedule_sync_from(bob.node_id) is False
    peers.network.emit_online(observer=alice.node_id, peer=bob.node_id)
    tick((alice,), space_id, time.monotonic() + TICK_SECONDS)
    sync.request_content("no-such-doc")
    settle(group, space_id)

    assert bob.node_id not in queried
    failures = [fields for event, fields in alice.events.seen[before:]
                if event == "orgfs.sync.failed" and fields.get("supplier") == bob.node_id]
    assert failures == []
    assert alice.runtime._supplier_sync_health.failure_count(bob.node_id) == 0  # noqa: SLF001
    assert sync._supplier_sync_local_health.failure_count(bob.node_id) == 0  # noqa: SLF001

    alice.fs.invite(space_id, bob.author, "rw")
    eventually(lambda: _check(bob.author in members(bob, space_id)))
    assert sync.schedule_sync_from(bob.node_id) is True
    settle(group, space_id)
    assert bob.node_id in queried


def test_a_removed_member_observation_does_not_decide_synced(peers: Peers) -> None:
    alice, bob, carol, space_id = team(peers)
    group = (alice, bob, carol)
    peers.disconnect(bob)
    alice.fs.remove_member(space_id, bob.author)
    eventually(lambda: _check(bob.author not in members(carol, space_id)))
    bob.fs.write_text(space_id, "node-bob.md", "written while removed but unaware")
    ahead = bob.fs.sync_host(space_id).read_vectors(("meta", "tree"))
    peers.reconnect(bob)
    eventually(lambda: _check(bob.author not in members(bob, space_id)))
    settle(group, space_id)
    tick(group, space_id, time.monotonic() + TICK_SECONDS)
    sync = alice.sync(space_id)
    with sync._worker_lock:  # noqa: SLF001 - plant the removed node's observation
        sync._observations[bob.node_id] = PeerObservation(  # noqa: SLF001
            bob.node_id, ahead["meta"], ahead["tree"], 0
        )

    assert bob.node_id not in {item.node for item in sync.observations()}
    assert synced(alice, space_id) is True
    assert alice.runtime is not None
    assert bob.node_id not in alice.runtime.status(space_id).holders_online
