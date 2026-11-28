# Decisions:
# - Package F6 (R3-1, R3-2): a receiver whose own author is not a current
#   member applies broadcast meta only and never counts the dropped frames;
#   frames the receive gate refuses as ``supplier-not-member`` are counted in
#   ``rejected_updates``, offline senders are not.
"""Broadcast publish / receive, bounded ingress, Pending outcomes."""

from __future__ import annotations

import json
import threading

import pytest

from hyprial.daemon.impl.orgfs.storage.blobs import BlobStore
from hyprial.daemon.impl.orgfs.sync import UpdateFrame, encode_update, encode_vector
from hyprial.daemon.impl.orgfs.sync.interface import SYNC_ENVELOPE_BYTES, SYNC_INGRESS_CAPACITY
from hyprial.daemon.impl.transport import KeySpace

from tests.daemon.orgfs.sync.support.bus import Bus
from tests.daemon.orgfs.sync.support.nodes import SPACE, make_node, same_state, settle, wait_for


@pytest.fixture
def bus() -> Bus:
    return Bus()


def _close(*nodes) -> None:
    for node in nodes:
        node.close()


def test_committed_update_is_broadcast_and_applied(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-1", "hello")
    settle(a, b)
    assert b.host.text("doc-1") == "hello"
    assert [kind for _doc, kind, _peer in b.host.applied] == ["Accepted", "Accepted"]
    assert bus.queries == []
    assert same_state(a, b)
    _close(a, b)


def test_new_document_never_shows_empty_content_when_content_beats_the_tree(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    bus.hold_puts = True
    a.host.write_text("doc-new", "first words")
    held = bus.take_held()
    assert [json.loads(p)["docId"] for _n, _k, p in held] == ["tree", "doc-new"]
    bus.hold_puts = False
    bus.deliver(*held[1])
    settle(a, b)
    assert ("doc-new", "Pending", "node-a") in b.host.applied
    wait_for(lambda: b.host.text("doc-new") == "first words")
    bus.deliver(*held[0])
    settle(a, b)
    assert all(text in (None, "first words") for doc, text in b.host.visible if doc == "doc-new")
    assert same_state(a, b)
    _close(a, b)


def test_missing_predecessor_pulls_from_the_same_peer_at_once(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    c = make_node(bus, "node-c")
    bus.hold_puts = True
    a.host.write_text("doc-1", "one")
    a.host.write_text("doc-1", "one two")
    held = bus.take_held()
    bus.hold_puts = False
    bus.partition("node-b", "node-c")
    for frame in held[2:]:
        bus.deliver(*frame)
    settle(a, b, c)
    assert ("tree", "Pending", "node-a") in b.host.applied
    assert any(key.endswith("/org/fs3/sync/space-1/node-a") and node == "node-b"
               for key, node, _s, _r in bus.queries)
    wait_for(lambda: b.host.text("doc-1") == "one two")
    assert not any(key.endswith("/node-c") for key, node, _s, _r in bus.queries if node == "node-b")
    _close(a, b, c)


def test_oversized_broadcast_carries_only_the_vector_and_the_peer_pulls(bus: Bus, tmp_path) -> None:
    a = make_node(bus, "node-a", blobs=BlobStore(tmp_path / "a"))
    b = make_node(bus, "node-b", blobs=BlobStore(tmp_path / "b"))
    bus.hold_puts = True
    a.host.write_text("doc-big", "Q" * (SYNC_ENVELOPE_BYTES + 1000))
    held = bus.take_held()
    bus.hold_puts = False
    content = json.loads(held[1][2])
    assert content["docId"] == "doc-big" and "update" not in content and "vector" in content
    assert len(held[1][2]) < 1024
    for frame in held:
        bus.deliver(*frame)
    settle(a, b)
    wait_for(lambda: b.host.text("doc-big") is not None, timeout=20)
    assert same_state(a, b)
    _close(a, b)


def test_vector_only_frame_already_covered_does_not_pull(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-1", "x")
    settle(a, b)
    bus.queries.clear()
    vector = a.host.read_vectors(["doc-1"])["doc-1"]
    frame = encode_update(UpdateFrame(SPACE, "node-a", "doc-1", vector, None))
    bus.deliver("node-a", KeySpace().orgfs3_update(SPACE), frame)
    settle(a, b)
    assert bus.queries == []
    _close(a, b)


def test_invalid_frames_are_refused_before_the_host(bus: Bus) -> None:
    b = make_node(bus, "node-b")
    key = KeySpace().orgfs3_update(SPACE)
    for payload in (
        b"not json",
        json.dumps({"schemaVersion": 1, "type": "orgfs-update"}).encode(),
        json.dumps({"schemaVersion": 3, "type": "update", "spaceId": SPACE, "node": "node-a",
                    "docId": "doc-1", "vector": "AA==", "update": "/wE="}).encode(),
    ):
        bus.deliver("node-x", key, payload)
    settle(b)
    assert b.host.calls == []
    assert sum(1 for _l, event, _f in b.events if event == "orgfs.update.rejected") == 3
    _close(b)


def test_node_gate_drops_frames_from_offline_or_non_member_nodes(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    b.online.discard("node-a")
    a.host.write_text("doc-1", "x")
    settle(a, b)
    b.online.add("node-a")
    b.host.denied_suppliers.add("node-a")
    a.host.write_text("doc-1", "y")
    settle(a, b)
    assert [c for c in b.host.calls if c[0] == "apply_remote"] == []
    reasons = [f["reason"] for _l, e, f in b.events if e == "orgfs.update.rejected"]
    assert reasons == ["supplier-offline", "supplier-offline", "supplier-not-member", "supplier-not-member"]
    assert b.sync.rejected_updates == 2
    _close(a, b)


def test_a_receiver_that_is_not_a_member_applies_broadcast_meta_only(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-1", "before")
    settle(a, b)
    b.host.members.discard("user:b")
    b.host.applied.clear()
    bus.queries.clear()

    a.host.write_text("doc-1", "after")
    a.host.write_text("doc-2", "new")
    a.host.set_meta("user:e", "rw")
    ahead = encode_vector({a.host.client_id: 1_000})
    bus.deliver("node-a", KeySpace().orgfs3_update(SPACE),
                encode_update(UpdateFrame(SPACE, "node-a", "doc-1", ahead, None)))
    settle(a, b)

    assert [doc_id for doc_id, _kind, _peer in b.host.applied] == ["meta"]
    assert b.host.meta().get("user:e") == "rw"
    assert b.host.text("doc-1") == "before"
    assert b.host.text("doc-2") is None
    assert bus.queries == []
    assert b.sync.rejected_updates == 0
    assert not any(event == "orgfs.update.rejected" for _l, event, _f in b.events)
    _close(a, b)


def test_ingress_is_bounded_forged_floods_drop_and_pulls_recover(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    gate = threading.Event()
    b.host.block = gate
    b.online.add("node-forged")
    b.host.denied_suppliers.add("node-forged")
    key = KeySpace().orgfs3_update(SPACE)
    forged = encode_update(UpdateFrame(SPACE, "node-forged", "meta", b"\x01\x05\x01", None))
    a.host.write_text("doc-0", "seed")
    for _ in range(SYNC_INGRESS_CAPACITY + 50):
        bus.deliver("node-forged", key, forged)
    a.host.write_text("doc-honest", "dropped?")
    gate.set()
    settle(a, b)
    assert b.sync.ingress_dropped >= 50 + 2
    assert b.host.text("doc-honest") is None
    b.sync.sync_from("node-a")
    b.sync.sync_from("node-a")
    assert same_state(a, b)
    assert not any(k.endswith("/node-forged") for k, _n, _s, _r in bus.queries)
    _close(a, b)


def test_own_broadcast_echo_is_ignored(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    payload = encode_update(UpdateFrame(SPACE, "node-a", "doc-1", b"\x01\x01\x01", None))
    bus.deliver("node-z", KeySpace().orgfs3_update(SPACE), payload)
    settle(a)
    assert a.host.calls == []
    _close(a)
