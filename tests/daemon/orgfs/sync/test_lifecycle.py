# Decisions:
# - Package F6 (R3-1): a blob supplier serves only requesters whose author is
#   a current member (``not-a-member`` otherwise, for a removed and for a
#   never-invited author alike); a node that is not a current member itself
#   requests no blob and asks for no content.
# - Package F8 (R5-1): close finishes the broadcast being applied and
#   discards the queued ones (counted in ``ingress_dropped``; the next pull
#   recovers them) instead of applying all of them first.
"""Close, rebuild, legacy keys, scheduling, maintain, announce, read-first, blobs."""

from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest

from hyprial.daemon.impl.orgfs.network.supplier_health import SupplierSyncHealth
from hyprial.daemon.impl.orgfs.storage.blobs import BlobStore
from hyprial.daemon.impl.orgfs.sync.interface import SYNC_BLOB_FETCH_QUEUE_LIMIT, SyncError
from hyprial.daemon.impl.transport import KeySpace

from tests.daemon.orgfs.sync.support.bus import Bus
from tests.daemon.orgfs.sync.support.nodes import SPACE, make_node, same_state, settle, wait_for


@pytest.fixture
def bus() -> Bus:
    return Bus()


def _close(*nodes) -> None:
    for node in nodes:
        node.close()


def test_close_discards_queued_broadcasts_then_never_calls_the_host(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    gate = threading.Event()
    b.host.block = gate
    for index in range(10):
        a.host.write_text(f"doc-{index}", "x")
    threading.Timer(0.2, gate.set).start()
    b.sync.close(timeout=5.0)
    assert sum(1 for name, _doc in b.host.calls if name == "apply_remote") == 1
    assert b.sync.ingress_dropped == 19
    b.host.closed = True
    a.host.write_text("doc-late", "late")
    assert not bus.sessions["node-b"].query(KeySpace().orgfs3_sync(SPACE, "node-b"), b"{}")
    a.sync.schedule_sync_from("node-b")
    settle(a)
    assert b.host.callbacks_after_close == 0
    assert b.sync.schedule_sync_from("node-a") is False
    b.sync.publish("doc-x", b"\x00\x00", b"\x00")
    b.sync.request_content("doc-x")
    b.sync.maintain(time.monotonic())
    assert b.host.callbacks_after_close == 0
    a.sync.close()


def test_close_raises_when_the_deadline_passes_with_work_in_flight(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    gate = threading.Event()
    b.host.block = gate
    a.host.write_text("doc-1", "x")
    with pytest.raises(TimeoutError):
        b.sync.close(timeout=0.1)
    gate.set()
    b.sync.close(timeout=5.0)
    a.sync.close()


def test_close_stops_a_round_before_its_next_host_call(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    for index in range(5):
        a.host.write_text(f"doc-{index}", "x", publish=False)
    b.sync.sync_from("node-a")
    settle(a, b)
    a.host.write_text("doc-9", "y", publish=False)
    gate = threading.Event()
    entered = threading.Event()
    original = b.host.apply_remote

    def slow(*args):
        entered.set()
        gate.wait(5)
        return original(*args)

    b.host.apply_remote = slow
    assert b.sync.schedule_sync_from("node-a")
    assert entered.wait(5)
    closer = threading.Thread(target=b.sync.close)
    closer.start()
    time.sleep(0.05)
    gate.set()
    closer.join(5)
    assert not closer.is_alive()
    calls = len(b.host.calls)
    b.host.closed = True
    time.sleep(0.1)
    assert len(b.host.calls) == calls
    a.sync.close()


def test_rebuild_redeclares_lost_handlers_and_subscriptions(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-1", "x", publish=False)
    bus.sessions["node-a"].lose_declarations()
    with pytest.raises(SyncError):
        b.sync.sync_from("node-a")
    bus.sessions["node-a"].finish_rebuild()
    settle(a)
    b.sync.sync_from("node-a")
    settle(a, b)
    assert same_state(a, b)
    b.host.write_text("doc-2", "pushed")
    settle(a, b)
    assert a.host.text("doc-2") == "pushed"
    assert any(event == "orgfs.sync.redeclared" for _l, event, _f in a.events)
    _close(a, b)


def test_legacy_journal_keys_have_no_subscriber_and_no_answer(bus: Bus, tmp_path) -> None:
    a = make_node(bus, "node-a", blobs=BlobStore(tmp_path / "a"))
    keys = KeySpace()
    declared = [expression for _node, expression, _cb in bus.subscribers + bus.handlers]
    assert all("/org/fs3/" in e or e == keys.prefix + "/org/fs/blob/*" for e in declared)
    probe = bus.session("node-probe")
    journal = f"{keys.prefix}/org/fs/{SPACE}"
    assert not probe.query(f"{journal}/sync/node-a", b"{}")
    assert not probe.query(f"{journal}/log/tree/w/*", b"{}")
    assert bus.deliver("node-probe", f"{journal}/log/tree/w/1", b"{}") == 0
    assert bus.deliver("node-probe", f"{journal}/log/x", b"{}") == 0
    assert a.host.calls == []
    a.close()


def test_blob_handler_answers_only_for_its_own_space(bus: Bus, tmp_path) -> None:
    store = BlobStore(tmp_path / "a")
    a = make_node(bus, "node-a", blobs=store)
    digest = store.put("space-other", b"secret", reason="ref")
    from hyprial.daemon.impl.orgfs.storage.blobs import encode_request

    probe = bus.session("node-probe")
    [reply] = probe.query(KeySpace().orgfs_blob(digest), encode_request(
        "space-other", digest, requester_node="node-probe", requester_author="user:a"
    ))
    assert json.loads(reply.payload)["code"] == "unknown-space"
    a.close()


def test_schedule_sync_from_coalesces_and_bounds_like_the_journal_mesh(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    gate = threading.Event()
    b.host.block = gate
    a.host.write_text("doc-1", "x", publish=False)
    assert b.sync.schedule_sync_from("node-a") is True
    wait_for(lambda: "node-a" in b.sync._sync_pending)
    assert b.sync.schedule_sync_from("node-a") is True
    assert b.sync.schedule_sync_from("node-a") is False
    assert b.sync.schedule_sync_from("node-b") is False
    gate.set()
    settle(a, b)
    assert same_state(a, b)
    _close(a, b)


def test_unreachable_supplier_is_held_after_three_failures(bus: Bus) -> None:
    health = SupplierSyncHealth()
    b = make_node(bus, "node-b", health=health)
    for _ in range(3):
        with pytest.raises(SyncError):
            b.sync.sync_from("node-gone")
    for _ in range(3):
        assert b.sync.schedule_sync_from("node-gone")
        settle(b)
    assert health.failure_count("node-gone") >= 3
    assert health.hold_remaining("node-gone") > 0
    assert b.sync.schedule_sync_from("node-gone") is False
    b.sync.close()


def test_read_first_request_puts_the_doc_at_the_head_of_the_next_round(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    for index in range(200):
        a.host.write_text(f"doc-{index:03}", "z", publish=False)
    gate = threading.Event()
    b.host.block = gate
    b.sync.schedule_sync_from("node-a")
    b.sync.request_content("doc-199")
    gate.set()
    settle(a, b)
    requests = [json.loads(q) for q, _rs in bus.requests("/org/fs3/sync/")]
    content = [list(r["vectors"])[2:] for r in requests if len(r["vectors"]) > 2]
    assert content[0][0] == "doc-199"
    assert all("doc-199" not in later for later in content[1:])
    assert max(len(c) for c in content) == 128
    assert b.host.text("doc-199") == "z"
    assert same_state(a, b)
    _close(a, b)


def test_maintain_drops_observations_of_offline_peers_and_retries_read_first(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-1", "x", publish=False)
    b.sync.sync_from("node-a")
    settle(a, b)
    assert [o.node for o in b.sync.observations()] == ["node-a"]
    b.online.discard("node-a")
    b.sync.maintain(time.monotonic())
    b.online.add("node-a")
    assert b.sync.observations() == ()
    a.host.write_text("doc-2", "y", publish=False)
    b.sync.request_content("doc-2")
    settle(a, b)
    assert same_state(a, b)
    _close(a, b)


def test_announce_lists_this_space_with_protocol_3(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    seen = []
    observer = bus.session("node-observer")
    observer.subscribe(KeySpace().orgfs_announce("node-a"), lambda sample: seen.append(sample))
    a.sync.announce()
    [sample] = seen
    value = json.loads(sample.payload)
    assert value["protocol"] == 3 and value["type"] == "orgfs-announce"
    assert value["node"] == "node-a" and value["schemaVersion"] == 1
    assert [space["spaceId"] for space in value["spaces"]] == [SPACE]
    a.sync.close()


def test_observation_generation_counts_local_commits_before_the_round(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    b.host.write_text("doc-1", "local", publish=True)
    settle(a, b)
    b.sync.sync_from("node-a")
    [observation] = b.sync.observations()
    assert observation.local_generation == 2
    assert observation.meta_vector == a.host.read_vectors(["meta"])["meta"]
    _close(a, b)


def test_observation_generation_comes_from_the_host_not_from_publish(bus: Bus) -> None:
    make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    b.host.write_text("doc-1", "unpublished", publish=False)
    b.host.generation += 5
    b.sync.sync_from("node-a")
    [observation] = b.sync.observations()
    assert observation.local_generation == 7
    assert ("local_generation", "") in b.host.calls


def test_fetch_blob_pulls_a_content_blob_from_a_holder_and_checks_its_digest(bus: Bus, tmp_path) -> None:
    store_a = BlobStore(tmp_path / "a")
    make_node(bus, "node-a", blobs=store_a)
    b = make_node(bus, "node-b", blobs=BlobStore(tmp_path / "b"))
    body = b"binary file " * 200_000
    digest = store_a.put(SPACE, body, reason="ref")

    assert b.sync.fetch_blob("node-a", digest) == body
    assert b.sync.fetch_blob("node-a", digest, size_hint=len(body)) == body
    assert hashlib.sha256(body).hexdigest() == digest


def test_fetch_blob_refuses_offline_and_non_member_holders(bus: Bus, tmp_path) -> None:
    store_a = BlobStore(tmp_path / "a")
    make_node(bus, "node-a", blobs=store_a)
    b = make_node(bus, "node-b", blobs=BlobStore(tmp_path / "b"))
    digest = store_a.put(SPACE, b"x", reason="ref")
    b.host.denied_suppliers.add("node-a")
    with pytest.raises(SyncError) as raised:
        b.sync.fetch_blob("node-a", digest)
    assert raised.value.code == "not-a-member"
    b.host.denied_suppliers.clear()
    b.online.discard("node-a")
    with pytest.raises(SyncError) as raised:
        b.sync.fetch_blob("node-a", digest)
    assert raised.value.code == "not-a-member"
    assert bus.queries == []


@pytest.mark.parametrize("removed", [True, False])
def test_blob_supplier_refuses_a_requester_that_is_not_a_current_member(
    bus: Bus, tmp_path, removed: bool
) -> None:
    store_a = BlobStore(tmp_path / "a")
    a = make_node(bus, "node-a", blobs=store_a)
    b = make_node(bus, "node-b", blobs=BlobStore(tmp_path / "b"), holders={"node-a"})
    digest = store_a.put(SPACE, b"secret", reason="ref")
    a.host.members.discard("user:b")
    if removed:
        a.host.removed.add("user:b")

    with pytest.raises(SyncError) as raised:
        b.sync.fetch_blob("node-a", digest)
    b.sync.request_blob(digest).wait(5.0)

    assert raised.value.code == "not-a-member"
    assert not b.blobs.contains(digest)
    (_sent, replies), *_rest = bus.requests("/org/fs/blob")
    reasons = {
        answer["details"].get("reason")
        for answer in map(json.loads, replies)
        if answer.get("code") == "not-a-member"
    }
    assert reasons == {"requester-removed" if removed else "requester-not-member"}
    _close(a, b)


def test_a_node_that_is_not_a_member_requests_no_blob_and_no_content(bus: Bus, tmp_path) -> None:
    store_a = BlobStore(tmp_path / "a")
    a = make_node(bus, "node-a", blobs=store_a)
    b = make_node(bus, "node-b", blobs=BlobStore(tmp_path / "b"), holders={"node-a"})
    digest = store_a.put(SPACE, b"secret", reason="ref")
    b.host.members.discard("user:b")
    bus.queries.clear()

    assert b.sync.request_blob(digest).is_set()
    with pytest.raises(SyncError) as raised:
        b.sync.fetch_blob("node-a", digest)
    b.sync.request_content("doc-1")
    b.sync.maintain(time.monotonic())
    settle(a, b)

    assert raised.value.details["reason"] == "local-not-member"
    assert bus.queries == []
    assert not b.blobs.contains(digest)
    _close(a, b)


def test_fetch_blob_absence_answers_never_decide(bus: Bus, tmp_path) -> None:
    make_node(bus, "node-a", blobs=BlobStore(tmp_path / "a"))
    b = make_node(bus, "node-b", blobs=BlobStore(tmp_path / "b"))
    with pytest.raises(SyncError) as raised:
        b.sync.fetch_blob("node-a", "f" * 64)
    assert raised.value.code == "blob-unavailable"


def test_request_blob_coalesces_fetches_and_stores_the_blob(bus: Bus, tmp_path) -> None:
    store_a = BlobStore(tmp_path / "a")
    store_b = BlobStore(tmp_path / "b")
    make_node(bus, "node-a", blobs=store_a)
    b = make_node(bus, "node-b", blobs=store_b, holders={"node-a"})
    digest = store_a.put(SPACE, b"payload", reason="ref")
    gate = threading.Event()
    original = b.sync._fetch_blob

    def slow(*args, **kwargs):
        gate.wait(5)
        return original(*args, **kwargs)

    b.sync._fetch_blob = slow
    first = b.sync.request_blob(digest)
    second = b.sync.request_blob(digest)
    assert first is second and not first.is_set()
    gate.set()
    assert first.wait(5)
    assert store_b.contains(digest)
    assert sum(1 for key, *_ in bus.queries if key.endswith(digest)) == 1
    assert b.sync.request_blob(digest).is_set()


def test_request_blob_queue_is_bounded(bus: Bus, tmp_path) -> None:
    make_node(bus, "node-a", blobs=BlobStore(tmp_path / "a"))
    b = make_node(bus, "node-b", blobs=BlobStore(tmp_path / "b"), holders={"node-a"})
    gate = threading.Event()

    def blocked(*_a, **_k):
        gate.wait(5)
        raise SyncError("blob-unavailable", "held")

    b.sync._fetch_blob = blocked
    events = [b.sync.request_blob(f"{i:064x}") for i in range(SYNC_BLOB_FETCH_QUEUE_LIMIT + 5)]
    assert [event.is_set() for event in events[-5:]] == [True] * 5
    assert not any(event.is_set() for event in events[:SYNC_BLOB_FETCH_QUEUE_LIMIT])
    dropped = [f for _l, e, f in b.events if e == "orgfs.content.blob-fetch-dropped"]
    assert len(dropped) == 5 and dropped[0]["reason"] == "queue-full"
    gate.set()
    assert all(event.wait(10) for event in events)


def test_request_blob_ends_at_once_without_a_holder(bus: Bus, tmp_path) -> None:
    b = make_node(bus, "node-b", blobs=BlobStore(tmp_path / "b"), holders=set())
    assert b.sync.request_blob("a" * 64).wait(5)
    assert bus.queries == []


def test_request_blob_without_store_or_after_close_is_already_set(bus: Bus, tmp_path) -> None:
    b = make_node(bus, "node-b")
    assert b.sync.request_blob("a" * 64).is_set()
    c = make_node(bus, "node-c", blobs=BlobStore(tmp_path / "c"))
    c.sync.close()
    assert c.sync.request_blob("a" * 64).is_set()


def test_request_blob_tries_discovered_holders_until_one_has_it(bus: Bus, tmp_path) -> None:
    store_c = BlobStore(tmp_path / "c")
    make_node(bus, "node-a", blobs=BlobStore(tmp_path / "a"))
    make_node(bus, "node-c", blobs=store_c)
    b = make_node(bus, "node-b", blobs=BlobStore(tmp_path / "b"), holders=lambda: ["node-a", "node-c"])
    digest = store_c.put(SPACE, b"only on c", reason="ref")
    bus.partition("node-b", "node-c")
    assert b.sync.request_blob(digest).wait(5)
    assert not b.blobs.contains(digest)
    bus.heal()
    assert b.sync.request_blob(digest).wait(5)
    assert b.blobs.contains(digest)


def test_announcement_source_makes_announce_node_level(bus: Bus) -> None:
    spaces = [{"spaceId": SPACE, "roles": ["member"], "mode": "rw"},
              {"spaceId": "space-2", "roles": ["member"], "mode": "ro"}]
    a = make_node(bus, "node-a", announcement_source=lambda: spaces)
    seen = []
    bus.session("node-observer").subscribe(KeySpace().orgfs_announce("node-a"), seen.append)
    a.sync.announce()
    value = json.loads(seen[-1].payload)
    assert value["protocol"] == 3 and value["spaces"] == spaces


def test_oversized_announcement_falls_back_to_space_ids(bus: Bus) -> None:
    spaces = [{"spaceId": f"space-{i}", "roles": ["member"], "mode": "rw", "pad": "x" * 30_000}
              for i in range(100)]
    a = make_node(bus, "node-a", announcement_source=lambda: spaces)
    seen = []
    bus.session("node-observer").subscribe(KeySpace().orgfs_announce("node-a"), seen.append)
    a.sync.announce()
    value = json.loads(seen[-1].payload)
    assert value["spaces"] == [{"spaceId": f"space-{i}"} for i in range(100)]


def test_holder_discovery_drives_read_first_requests(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b", holders={"node-a"})
    a.host.write_text("doc-1", "x", publish=False)
    b.sync.request_content("doc-1")
    settle(a, b)
    assert b.host.text("doc-1") == "x"
