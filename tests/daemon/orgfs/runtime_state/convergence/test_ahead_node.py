# Decisions (package F7, real-node finding R4-3, 2026-11-25):
# - A broadcast is sent once, at commit.  A node holding updates nobody else
#   has (the broadcast was lost, or refused then and acceptable now) must
#   still reach every online member without a further write and without a
#   restart: the due tick pulls every member holder whose observation does
#   not cover the local vectors, and a supplier whose local vectors do not
#   cover a member requester's pulls back from it.
# - The R4 run: bob is partitioned, alice removes him, bob (not yet aware)
#   rewrites a document and creates one, the partition heals and bob learns
#   the removal, then alice re-invites bob.  Result-state admission is the
#   established rule and stays: bob's writes made while removed but unaware
#   are accepted after the re-invite under the then-current member table,
#   with bob as their author.
# - "Nobody reports synced:true while behind" is checked at every quiescent
#   point after the re-invite: a node whose documents differ from the final
#   state must not report ``synced`` True.  Before a node has any evidence
#   that a peer is ahead (an observation or a pull request) it cannot know;
#   the separate test pins that a served pull request is such evidence.  A
#   lost broadcast leaves the others no evidence until the first due tick,
#   so that run checks from the first tick on (``informed=False``).
# - Bounded time is counted in due ticks (``tick``), each far past the
#   longest catalog backoff; the R4 re-invite and the lost broadcast must
#   converge within one tick.
# - No storm: after convergence, further ticks send no pull, broadcast or
#   announcement at all.
"""A node ahead of online members makes them pull, without a write or restart."""

from __future__ import annotations

from threading import Event
import time
from typing import Any

from ..support import Peer, Peers, eventually
from .team import TICK_SECONDS, Traffic, contents, members, settle, synced, team, tick


def _check(condition: bool) -> None:
    assert condition


def _drop_broadcasts(peer: Peer) -> Event:
    assert peer.runtime is not None
    session = peer.runtime._session  # noqa: SLF001 - the node's transport
    original = session.put
    dropping = Event()

    def put(key: str, payload: bytes) -> None:
        if dropping.is_set() and "/org/fs3/update/" in key:
            return
        original(key, payload)

    session.put = put
    return dropping


def _held_pulls(peer: Peer, space_id: str) -> tuple[list[str], Event]:
    sync = peer.sync(space_id)
    original = sync.schedule_sync_from
    held: list[str] = []
    holding = Event()
    holding.set()

    def schedule(node: str) -> bool:
        if holding.is_set():
            held.append(node)
            return True
        return original(node)

    sync.schedule_sync_from = schedule
    return held, holding


def _no_node_behind_reports_synced(group: tuple[Peer, ...], space_id: str, expected: dict[str, str]) -> None:
    for peer in group:
        if contents(peer, space_id) != expected:
            assert synced(peer, space_id) is not True, peer.node_id


def _converge_by_ticks(
    group: tuple[Peer, ...], space_id: str, expected: dict[str, str], limit: int, *, informed: bool = True
) -> int:
    now = time.monotonic()
    for ticks in range(limit + 1):
        settle(group, space_id)
        if informed or ticks:
            _no_node_behind_reports_synced(group, space_id, expected)
        if all(contents(peer, space_id) == expected for peer in group):
            return ticks
        now += TICK_SECONDS
        tick(group, space_id, now)
    raise AssertionError({peer.node_id: contents(peer, space_id) for peer in group})


def test_a_member_re_invited_after_writing_in_a_partition_converges_without_a_restart(peers: Peers) -> None:
    alice, bob, carol, space_id = team(peers)
    group = (alice, bob, carol)
    peers.disconnect(bob)
    alice.fs.remove_member(space_id, bob.author)
    eventually(lambda: _check(bob.author not in members(carol, space_id)))
    bob.fs.write_text(space_id, "node-bob.md", "rewritten during the partition")
    bob.fs.write_text(space_id, "b-part.md", "created during the partition")
    expected = contents(bob, space_id)
    peers.reconnect(bob)
    eventually(lambda: _check(bob.author not in members(bob, space_id)))
    settle(group, space_id)
    assert contents(alice, space_id)["node-bob.md"] == "from node-bob"
    assert "b-part.md" not in contents(carol, space_id)

    alice.fs.invite(space_id, bob.author, "rw")

    assert _converge_by_ticks(group, space_id, expected, limit=3) <= 1
    for peer in (alice, carol):
        assert peer.fs.stat(space_id, "b-part.md").modified_by == bob.author
    now = time.monotonic() + 10 * TICK_SECONDS
    for _ in range(2):
        now += TICK_SECONDS
        tick(group, space_id, now)
    assert [synced(peer, space_id) for peer in group] == [True, True, True]


def test_a_lost_broadcast_reaches_every_member_without_a_further_write(peers: Peers) -> None:
    alice, bob, carol, space_id = team(peers)
    group = (alice, bob, carol)
    dropping = _drop_broadcasts(carol)
    dropping.set()
    carol.fs.write_text(space_id, "lost.md", "its broadcast was lost")
    dropping.clear()
    settle(group, space_id)
    expected = contents(carol, space_id)
    assert "lost.md" not in contents(alice, space_id)
    assert "lost.md" not in contents(bob, space_id)

    assert _converge_by_ticks(group, space_id, expected, limit=3, informed=False) <= 1


def test_pulls_and_announcements_stop_growing_after_convergence(peers: Peers) -> None:
    alice, bob, carol, space_id = team(peers)
    group = (alice, bob, carol)
    dropping = _drop_broadcasts(bob)
    dropping.set()
    bob.fs.write_text(space_id, "lost.md", "its broadcast was lost")
    dropping.clear()
    expected = contents(bob, space_id)
    traffic = Traffic(group)
    _converge_by_ticks(group, space_id, expected, limit=3, informed=False)

    now = time.monotonic() + 10 * TICK_SECONDS
    seen: list[dict[Any, int]] = []
    for _ in range(6):
        now += TICK_SECONDS
        tick(group, space_id, now)
        seen.append(traffic.snapshot())

    assert seen[2] == seen[-1]
    assert [synced(peer, space_id) for peer in group] == [True, True, True]


def test_a_member_that_served_a_requester_ahead_of_it_is_not_synced_until_it_pulled(peers: Peers) -> None:
    alice, bob, carol, space_id = team(peers)
    group = (alice, bob, carol)
    assert synced(alice, space_id) is True
    dropping = _drop_broadcasts(bob)
    dropping.set()
    bob.fs.write_text(space_id, "ahead.md", "only on bob")
    dropping.clear()
    held, holding = _held_pulls(alice, space_id)

    bob.sync(space_id).sync_from(alice.node_id)
    settle(group, space_id)

    assert bob.node_id in held
    assert "ahead.md" not in contents(alice, space_id)
    assert synced(alice, space_id) is False
    holding.clear()
    tick(group, space_id, time.monotonic() + TICK_SECONDS)
    eventually(lambda: _check(contents(alice, space_id).get("ahead.md") == "only on bob"))
    eventually(lambda: _check(synced(alice, space_id) is True))
