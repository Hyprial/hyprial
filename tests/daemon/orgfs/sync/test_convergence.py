"""Multi-node convergence (C7 triage §5.3 items 8, 9, 14, 15, 16, 20)."""

from __future__ import annotations

import json
import random

import pytest
from pycrdt import Map, Text

from tests.daemon.orgfs.sync.support.bus import Bus
from tests.daemon.orgfs.sync.support.host import MemoryHost
from tests.daemon.orgfs.sync.support.nodes import converge, make_node, same_state, settle


@pytest.fixture
def bus() -> Bus:
    return Bus()


def _close(*nodes) -> None:
    for node in nodes:
        node.close()


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_reordered_duplicated_dropped_and_partitioned_broadcasts_converge(bus: Bus, seed: int) -> None:
    """Content docs have one writer each; meta and tree are written by all.

    Concurrent writers of one content doc are covered by the xfail below.
    """

    rng = random.Random(seed)
    nodes = [make_node(bus, f"node-{x}") for x in "abcd"]
    bus.hold_puts = True
    for step in range(60):
        writer = rng.choice(nodes)
        doc_id = f"doc-{writer.name[-1]}{rng.randrange(3)}"
        if rng.random() < 0.15:
            writer.host.set_meta(f"user:{rng.randrange(5)}", rng.choice(["rw", "ro", None]))
        elif rng.random() < 0.1 and writer.host.text(doc_id) is not None:
            writer.host.clear_text(doc_id)
        else:
            writer.host.write_text(doc_id, f"{writer.name} step {step}")
        if step == 20:
            bus.partition("node-a", "node-c")
            bus.partition("node-b", "node-d")
        frames = bus.take_held()
        rng.shuffle(frames)
        for frame in frames:
            roll = rng.random()
            if roll < 0.2:
                continue
            bus.deliver(*frame)
            if roll > 0.8:
                bus.deliver(*frame)
    bus.hold_puts = False
    settle(*nodes)
    bus.heal()
    converge(nodes)
    assert same_state(*nodes)
    _close(*nodes)


def test_pure_deletions_reach_a_peer_that_missed_the_broadcast(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-1", "to be cleared")
    a.host.set_meta("user:gone", "rw")
    settle(a, b)
    assert b.host.text("doc-1") == "to be cleared" and "user:gone" in b.host.meta()

    bus.partition("node-a", "node-b")
    a.host.clear_text("doc-1")
    a.host.set_meta("user:gone", None)
    settle(a, b)
    assert b.host.text("doc-1") == "to be cleared" and "user:gone" in b.host.meta()
    bus.heal()
    b.sync.sync_from("node-a")
    settle(a, b)
    assert str(b.host.docs["doc-1"].get("body", type=Text)) == ""
    assert "user:gone" not in b.host.meta()
    assert same_state(a, b)
    _close(a, b)


def test_pure_deletion_without_the_commit_clock_would_not_propagate(bus: Bus) -> None:
    """Documents why §3.3's clock is load-bearing: vectors ignore deletes."""

    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-1", "abc")
    settle(a, b)
    bus.partition("node-a", "node-b")
    a.host.clear_text("doc-1", clock=False)
    bus.heal()
    b.sync.request_content("doc-1")
    b.sync.sync_from("node-a")
    settle(a, b)
    assert str(b.host.docs["doc-1"].get("body", type=Text)) == "abc"
    _close(a, b)


def test_1500_documents_converge_in_bounded_rounds_with_monotonic_progress(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    for index in range(1500):
        a.host.write_text(f"doc-{index:04}", f"body {index}", publish=False)
    b.sync.sync_from("node-a")
    settle(a, b, timeout=60)
    replies = [json.loads(r) for _q, rs in bus.requests("/org/fs3/sync/") for r in rs]
    progress = []
    arrived = 0
    for reply in replies[1:]:
        arrived += len(reply["updates"])
        progress.append(arrived)
    assert progress == sorted(progress) and all(len(r["updates"]) > 0 for r in replies)
    assert len(replies) <= 2 + -(-1500 // 128)
    assert b.host.pending_content(10) == ()
    assert same_state(a, b)
    _close(a, b)


def test_commit_not_broadcast_before_restart_is_pulled_by_the_peer(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-1", "before")
    settle(a, b)
    a.host.write_text("doc-1", "after, never broadcast", publish=False)
    a = a.restart(bus)
    assert a.host.text("doc-1") == "after, never broadcast"
    assert b.sync.schedule_sync_from("node-a")
    settle(a, b)
    assert b.host.text("doc-1") == "after, never broadcast"
    assert same_state(a, b)
    _close(a, b)


def test_left_space_stays_unsynced_across_restart_and_resumes_on_rejoin(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-1", "v1")
    settle(a, b)
    b.host.left = True
    a.host.write_text("doc-1", "v2")
    settle(a, b)
    b.sync.schedule_sync_from("node-a")
    settle(a, b)
    b = b.restart(bus)
    b.host.left = True
    b.sync.schedule_sync_from("node-a")
    a.host.write_text("doc-1", "v3")
    settle(a, b)
    assert b.host.text("doc-1") == "v1"
    assert not any(kind == "Accepted" for _d, kind, _p in b.host.applied)

    b.host.left = False
    b.sync.schedule_sync_from("node-a")
    settle(a, b)
    assert same_state(a, b)
    _close(a, b)


def test_meta_member_rows_converge_with_concurrent_writers(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    bus.partition("node-a", "node-b")
    a.host.set_meta("user:x", "rw")
    b.host.set_meta("user:y", "ro")
    b.host.commit("meta", lambda doc: doc.get("space", type=Map).__setitem__("kind", "team"))
    bus.heal()
    converge([a, b])
    assert a.host.meta() == {"user:x": "rw", "user:y": "ro"}
    assert same_state(a, b)
    _close(a, b)


def test_concurrent_edits_of_one_document_converge_after_a_partition_heals(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-1", "base")
    settle(a, b)
    bus.partition("node-a", "node-b")
    a.host.write_text("doc-1", "from a")
    b.host.write_text("doc-1", "from b")
    bus.heal()
    converge([a, b])
    assert same_state(a, b)
    assert a.host.frontier_repairs + b.host.frontier_repairs >= 1
    _close(a, b)


def test_three_nodes_editing_one_document_concurrently_converge(bus: Bus) -> None:
    nodes = [make_node(bus, f"node-{x}") for x in "abc"]
    nodes[0].host.write_text("doc-1", "base")
    settle(*nodes)
    for left in nodes:
        for right in nodes:
            if left is not right:
                bus.partition(left.name, right.name)
    for node in nodes:
        node.host.write_text("doc-1", f"from {node.name}")
    bus.heal()
    converge(nodes)
    assert same_state(*nodes)
    text = nodes[0].host.text("doc-1")
    assert all(f"from {node.name}" in text for node in nodes)
    _close(*nodes)


def test_frontier_repair_terminates_after_convergence(bus: Bus) -> None:
    nodes = [make_node(bus, f"node-{x}") for x in "abc"]
    nodes[0].host.write_text("doc-1", "base")
    settle(*nodes)
    bus.partition("node-a", "node-b")
    bus.partition("node-b", "node-c")
    for node in nodes:
        node.host.write_text("doc-1", f"from {node.name}")
    bus.heal()
    converge(nodes)
    repairs = [node.host.frontier_repairs for node in nodes]
    trees = [node.host.vectors()["tree"] for node in nodes]
    for _ in range(5):
        for node in nodes:
            for other in nodes:
                if other is not node:
                    assert node.sync.sync_from(other.name) == 0
        settle(*nodes)
    assert [node.host.frontier_repairs for node in nodes] == repairs
    assert [node.host.vectors()["tree"] for node in nodes] == trees
    assert same_state(*nodes)
    _close(*nodes)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_concurrent_writers_of_shared_documents_converge_under_faults(bus: Bus, seed: int) -> None:
    rng = random.Random(seed)
    nodes = [make_node(bus, f"node-{x}") for x in "abcd"]
    bus.hold_puts = True
    for step in range(60):
        writer = rng.choice(nodes)
        doc_id = f"doc-{rng.randrange(4)}"
        if rng.random() < 0.1 and writer.host.text(doc_id) is not None:
            writer.host.clear_text(doc_id)
        else:
            writer.host.write_text(doc_id, f"{writer.name} step {step}")
        if step == 20:
            bus.partition("node-a", "node-c")
            bus.partition("node-b", "node-d")
        frames = bus.take_held()
        rng.shuffle(frames)
        for frame in frames:
            roll = rng.random()
            if roll < 0.2:
                continue
            bus.deliver(*frame)
            if roll > 0.8:
                bus.deliver(*frame)
    bus.hold_puts = False
    settle(*nodes)
    bus.heal()
    converge(nodes)
    assert same_state(*nodes)
    _close(*nodes)


def _repair_beats_a_newer_row(bus: Bus, *, as_written: bool) -> tuple[bool, dict, dict]:
    """A merges C's v1 and repairs the row while C writes v2; A's repair wins the Map.

    Client ids are fixed so the Yjs Map conflict resolves the same way every run
    (A=2 beats C=1).  C's v2 content broadcast is lost; only tree frames arrive.
    """

    a = make_node(bus, "node-a", host=MemoryHost("node-a", client_id=2))
    c = make_node(bus, "node-c", host=MemoryHost("node-c", client_id=1))
    for node in (a, c):
        node.host.repair_after_tree = not as_written
        node.host.repair_union = not as_written
    c.host.write_text("doc-1", "v0")
    settle(a, c)
    bus.hold_puts = True
    c.host.write_text("doc-1", "v1")
    first = bus.take_held()
    bus.deliver(*next(frame for frame in first if json.loads(frame[2])["docId"] == "doc-1"))
    settle(a, c)
    repair = bus.take_held()
    c.host.write_text("doc-1", "v2")
    later = bus.take_held()
    bus.hold_puts = False
    for frame in first + repair + [f for f in later if json.loads(f[2])["docId"] == "tree"]:
        bus.deliver(*frame)
    settle(a, c)
    converge([a, c])
    result = (same_state(a, c), a.host.texts(), c.host.texts())
    _close(a, c)
    return result


def test_frontier_repair_as_written_in_4_2_diverges_when_a_repair_beats_a_newer_row(bus: Bus) -> None:
    """Reproduction for the report: §4.2's rule (content trigger, write the local vector)."""

    converged, a_texts, c_texts = _repair_beats_a_newer_row(bus, as_written=True)
    assert not converged
    assert a_texts == {"doc-1": "v1"} and c_texts == {"doc-1": "v2"}


def test_frontier_repair_with_tree_trigger_and_union_converges_in_the_same_race(bus: Bus) -> None:
    converged, a_texts, c_texts = _repair_beats_a_newer_row(bus, as_written=False)
    assert converged
    assert a_texts == c_texts == {"doc-1": "v2"}
