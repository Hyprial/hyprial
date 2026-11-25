"""Probe GC0: the owner-home space across two nodes of one owner, as of 2026-11-18.

These tests pin today's behaviour; they are facts, not a target.  Write-up:
``docs/design/chat-space-two-node-probe-2026-11-18.md``.

Decision notes (slice GC0):

- Every negative assertion has a positive control first: the other node's
  announce has reached this node and named it as a holder of the space, so
  "not held" cannot be explained by a link that was never up.
- Discovery is not membership: an announce only records which node holds
  which space id.  Nothing on the receiving node joins, pulls or creates a
  store for a space it does not already hold, and no daemon path joins an
  owner home (production ``join`` callers are org directory and personal
  org spaces only).
- ``join`` by space id is enough for a second node of the same owner: the
  owner is already a member of its own home, so no invite is needed.
- Repair after a gap is pinned on both edges: the supplier-online edge pulls
  every space the node holds; the periodic tick covers org directory and
  ACL spaces only, with an org directory space as its control.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.orgfs.composition import directory as directory_composition
from hyprial.identity import org_space_name
from tests.daemon.conversations.two_node.support import (
    NODE_A,
    NODE_B,
    OWNER,
    announce_until_held,
    broadcast_drained,
    close,
    drained,
    entry,
    eventually,
    expect_message_ids,
    ingress_drained,
    read_message_ids,
    record,
    start_node,
    start_pair,
)

CELL = "c.probe"
OTHER_CELL = "c.other"
INTERVAL = 60.0


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    monkeypatch.setattr(directory_composition, "_directory_sync_jitter", lambda: 0.0)


def _line(message_id: str, at_ms: int):
    return entry(CELL, message_id, at_ms, sender="agent:a", recipient="agent:b")


def _other(message_id: str, at_ms: int):
    return entry(OTHER_CELL, message_id, at_ms, sender="agent:a", recipient="agent:b")


def test_second_node_does_not_hold_first_nodes_home_without_join(tmp_path) -> None:
    _bus, a, b = start_pair(tmp_path)
    try:
        home = record(a, CELL, [_line("m1", 1)])
        assert a.homes() == (home,)
        announce_until_held(a, home, b)
        b.presence.emit_online(NODE_A)
        eventually(lambda: drained(b))

        assert home not in b.held_spaces()
        assert home not in {info.space_id for info in b.fs.spaces()}
        assert b.homes() == ()
        assert read_message_ids(b, CELL) == []
        assert read_message_ids(a, CELL) == ["m1"]
    finally:
        close(b, a)


def test_two_homes_created_before_any_join_stay_separate_and_writers_diverge(tmp_path) -> None:
    _bus, a, b = start_pair(tmp_path)
    try:
        home_b = record(b, CELL, [_line("m-b", 1)])
        home_a = record(a, CELL, [_line("m-a", 2)])
        announce_until_held(a, home_a, b)
        announce_until_held(b, home_b, a)
        for node, peer in ((a, NODE_B), (b, NODE_A)):
            node.presence.emit_online(peer)
            eventually(lambda node=node: drained(node))

        assert home_a != home_b
        assert a.homes() == (home_a,)
        assert b.homes() == (home_b,)
        assert (a.write_space(), b.write_space()) == (home_a, home_b)
        assert read_message_ids(a, CELL) == ["m-a"]
        assert read_message_ids(b, CELL) == ["m-b"]
    finally:
        close(b, a)


def test_an_explicit_join_needs_no_invite_and_converges_reads_and_writes(tmp_path) -> None:
    _bus, a, b = start_pair(tmp_path)
    try:
        home_b = record(b, CELL, [_line("m-b", 1)])
        home_a = record(a, CELL, [_line("m-a", 2)])
        announce_until_held(a, home_a, b)
        announce_until_held(b, home_b, a)

        assert b.fs.join(home_a).owner == OWNER
        assert a.fs.join(home_b).owner == OWNER

        both = tuple(sorted((home_a, home_b)))
        assert a.homes() == b.homes() == both
        assert a.write_space() == b.write_space() == both[0]
        assert read_message_ids(a, CELL) == read_message_ids(b, CELL) == ["m-a", "m-b"]

        record(b, CELL, [_line("m-b2", 3)], seq=0)
        eventually(lambda: expect_message_ids(a, CELL, ["m-a", "m-b", "m-b2"]))
    finally:
        close(b, a)


def test_a_write_made_while_the_holder_was_offline_is_pulled_on_its_online_edge(tmp_path) -> None:
    bus, a, b = start_pair(tmp_path)
    restarted = None
    try:
        home = record(a, CELL, [_line("m1", 1)])
        announce_until_held(a, home, b)
        b.fs.join(home)
        assert read_message_ids(b, CELL) == ["m1"]
        close(b)

        record(a, CELL, [_line("m2", 2)], seq=0)
        restarted = start_node(bus, tmp_path, NODE_B, NODE_A)
        assert home in restarted.held_spaces()
        assert read_message_ids(restarted, CELL) == ["m1"]

        restarted.presence.emit_online(NODE_A)
        eventually(lambda: expect_message_ids(restarted, CELL, ["m1", "m2"]))
    finally:
        close(restarted, a)


def test_a_live_update_lost_while_both_online_is_not_pulled_by_the_periodic_tick(tmp_path) -> None:
    bus, a, b = start_pair(tmp_path)
    try:
        home = record(a, CELL, [_line("m1", 1)])
        announce_until_held(a, home, b)
        b.fs.join(home)
        control = a.fs.create_space(org_space_name("group-probe"))
        announce_until_held(a, control.space_id, b)
        b.fs.join(control.space_id)
        assert b.runtime.anti_entropy_tick(0.0) == 0

        bus.drop_updates = True
        record(a, CELL, [_line("m2", 2)], seq=0)
        lost = a.fs.write_text(control.space_id, "lost.md", "lost")
        eventually(lambda: broadcast_drained(a, home, control.space_id))
        bus.drop_updates = False
        record(a, OTHER_CELL, [_other("m3", 3)])
        eventually(lambda: broadcast_drained(a, home))
        eventually(lambda: ingress_drained(b, home))
        assert read_message_ids(b, CELL) == ["m1"]
        assert read_message_ids(b, OTHER_CELL) == []

        assert b.runtime.anti_entropy_tick(INTERVAL) == 1
        eventually(lambda: b.fs.read_text(control.space_id, f"id:{lost.node_id}"))
        assert read_message_ids(b, CELL) == ["m1"]
        assert read_message_ids(b, OTHER_CELL) == []

        b.presence.emit_online(NODE_A)
        eventually(lambda: expect_message_ids(b, CELL, ["m1", "m2"]))
        eventually(lambda: expect_message_ids(b, OTHER_CELL, ["m3"]))
    finally:
        close(b, a)
