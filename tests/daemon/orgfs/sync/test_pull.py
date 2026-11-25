"""Pull rounds: catalog first, paged content, blob fallback, supplier gates."""

from __future__ import annotations

import base64
import json
import time

import pytest

from hyprial.daemon.impl.orgfs.storage.blobs import BlobStore
from hyprial.daemon.impl.orgfs.sync import SyncError
from hyprial.daemon.impl.orgfs.sync.interface import ORGFS_PAGE_BYTES, SYNC_DEPENDENCY_RETRY_LIMIT
from hyprial.daemon.impl.transport import KeySpace

from tests.daemon.orgfs.sync.support.bus import Bus
from tests.daemon.orgfs.sync.support.nodes import SPACE, converge, make_node, same_state, settle
from tests.daemon.orgfs.sync.support.outcomes import Pending, Rejected


@pytest.fixture
def bus() -> Bus:
    return Bus()


def _close(*nodes) -> None:
    for node in nodes:
        node.close()


def test_empty_node_clones_catalog_then_content(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.set_meta("user:a", "rw", publish=False)
    for index in range(5):
        a.host.write_text(f"doc-{index}", f"body {index}", publish=False)

    assert b.sync.sync_from("node-a") == 2
    settle(a, b)
    rounds = bus.requests("/org/fs3/sync/")
    first_request, [first_reply] = rounds[0]
    assert list(json.loads(first_request)["vectors"]) == ["meta", "tree"]
    assert [u["docId"] for u in json.loads(first_reply)["updates"]] == ["meta", "tree"]
    second_request, _ = rounds[1]
    assert list(json.loads(second_request)["vectors"]) == ["meta", "tree"] + [f"doc-{i}" for i in range(5)]
    assert len(rounds) == 2
    assert same_state(a, b)
    assert [doc_id for doc_id, _kind, _peer in b.host.applied][:2] == ["meta", "tree"]
    _close(a, b)


def test_full_clone_and_incremental_catch_up_reach_the_same_state(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    for index in range(20):
        a.host.write_text(f"doc-{index:02}", "v1", publish=False)
    converge([a, b])
    for index in range(0, 20, 3):
        a.host.write_text(f"doc-{index:02}", "v2", publish=False)
    a.host.set_meta("user:b", "ro", publish=False)
    converge([a, b])

    c = make_node(bus, "node-c")
    converge([c, a], rounds=10)
    assert same_state(a, b, c)
    _close(a, b, c)


def test_unchanged_round_sends_only_catalog_vectors_independent_of_doc_count(bus: Bus) -> None:
    sizes = []
    for count in (3, 300):
        local = Bus()
        a = make_node(local, "node-a")
        b = make_node(local, "node-b")
        for index in range(count):
            a.host.write_text(f"doc-{index:04}", "x", publish=False)
        converge([b, a], rounds=10)
        local.queries.clear()
        local.payloads.clear()
        assert b.sync.sync_from("node-a") == 0
        settle(a, b)
        [(sent, received)] = local.query_bytes("/org/fs3/sync/")
        request = json.loads(local.requests("/org/fs3/sync/")[-1][0])
        assert set(request["vectors"]) == {"meta", "tree"}
        sizes.append((sent, received))
        _close(a, b)
    (small_sent, small_received), (big_sent, big_received) = sizes
    assert big_sent - small_sent <= 8
    assert big_received - small_received <= 8


def test_content_pages_are_bounded_and_omitted_docs_come_next_round(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    text = "y" * 300_000
    for index in range(6):
        a.host.write_text(f"doc-{index}", text, publish=False)
    b.sync.sync_from("node-a")
    settle(a, b)
    replies = [json.loads(r) for _q, rs in bus.requests("/org/fs3/sync/") for r in rs]
    assert len(replies) >= 4
    assert all(r["omitted"] for r in replies[1:-1])
    assert replies[-1]["omitted"] == []
    for _key, _node, _sent, received in bus.queries:
        assert received <= ORGFS_PAGE_BYTES
    assert same_state(a, b)
    _close(a, b)


def test_requests_name_at_most_128_pending_content_docs(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    for index in range(200):
        a.host.write_text(f"doc-{index:03}", "z", publish=False)
    b.sync.sync_from("node-a")
    settle(a, b)
    requests = [json.loads(q) for q, _rs in bus.requests("/org/fs3/sync/")]
    assert [len(r["vectors"]) for r in requests] == [2, 2 + 128, 2 + 72]
    assert b.host.pending_content(500) == ()
    assert same_state(a, b)
    _close(a, b)


def test_large_update_travels_as_a_verified_temporary_blob(bus: Bus, tmp_path) -> None:
    store_a = BlobStore(tmp_path / "a")
    store_b = BlobStore(tmp_path / "b")
    a = make_node(bus, "node-a", blobs=store_a)
    b = make_node(bus, "node-b", blobs=store_b)
    a.host.write_text("doc-big", "B" * (ORGFS_PAGE_BYTES // 2 + 10), publish=False)

    converge([b, a], rounds=5)
    assert same_state(a, b)
    refs = store_a.references(SPACE)
    assert refs and all(reason == "sync" for _space, _digest, reason in refs)
    a.sync.maintain(time.monotonic() + 601)
    assert store_a.references(SPACE) == ()
    _close(a, b)


def test_expiry_unpins_only_the_sync_reason_of_a_shared_digest(bus: Bus, tmp_path) -> None:
    store_a = BlobStore(tmp_path / "a")
    a = make_node(bus, "node-a", blobs=store_a)
    b = make_node(bus, "node-b", blobs=BlobStore(tmp_path / "b"))
    a.host.write_text("doc-big", "E" * (ORGFS_PAGE_BYTES // 2 + 10), publish=False)

    converge([b, a], rounds=5)
    assert same_state(a, b)
    digests = {digest for _space, digest, _reason in store_a.references(SPACE)}
    assert len(digests) == 1
    (digest,) = digests
    store_a.pin(SPACE, digest, "own-write")
    a.sync.maintain(time.monotonic() + 601)
    assert store_a.references(SPACE) == ((SPACE, digest, "own-write"),)
    assert store_a.has(digest)
    _close(a, b)


def test_tampered_blob_is_refused_and_nothing_is_applied(bus: Bus, tmp_path) -> None:
    a = make_node(bus, "node-a", blobs=BlobStore(tmp_path / "a"))
    b = make_node(bus, "node-b", blobs=BlobStore(tmp_path / "b"))
    a.host.write_text("doc-big", "C" * (ORGFS_PAGE_BYTES // 2 + 10), publish=False)

    def tamper(key: str, body: bytes) -> bytes:
        if "/org/fs/blob/" not in key:
            return body
        value = json.loads(body)
        if value.get("type") == "orgfs-blob-chunk":
            value["data"] = value["data"][:-8] + "AAAAAAAA"
        return json.dumps(value).encode()

    bus.reply_filter = tamper
    b.sync.sync_from("node-a")
    settle(a, b)
    assert b.host.text("doc-big") is None
    assert "doc-big" not in [doc for doc, _kind, _peer in b.host.applied]
    assert any(event == "orgfs.sync.blob-fetch-failed" for _lvl, event, _f in b.events)
    bus.reply_filter = None
    b.sync.sync_from("node-a")
    settle(a, b)
    assert same_state(a, b)
    _close(a, b)


def test_large_update_without_a_blob_store_is_omitted_not_inlined(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-big", "D" * (ORGFS_PAGE_BYTES // 2 + 10), publish=False)
    b.sync.sync_from("node-a")
    settle(a, b)
    b.sync.sync_from("node-a")
    settle(a, b)
    assert b.host.text("doc-big") is None
    replies = [json.loads(r) for _q, rs in bus.requests("/org/fs3/sync/") for r in rs]
    assert replies[-1]["omitted"] == ["doc-big"]
    assert all(u["docId"] != "doc-big" for r in replies for u in r["updates"])
    _close(a, b)


def test_supplier_refuses_a_requester_that_is_not_a_member(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-1", "secret", publish=False)
    a.host.members.discard("user:b")

    with pytest.raises(SyncError) as raised:
        b.sync.sync_from("node-a")
    assert raised.value.code == "not-a-member"
    assert b.host.applied == []
    assert ("read_update", "meta") not in a.host.calls
    _close(a, b)


def test_supplier_answers_a_removed_requester_with_meta_only(bus: Bus, tmp_path) -> None:
    a = make_node(bus, "node-a", blobs=BlobStore(tmp_path / "a"))
    b = make_node(bus, "node-b", blobs=BlobStore(tmp_path / "b"))
    a.host.set_meta("user:a", "rw", publish=False)
    a.host.write_text("doc-1", "before", publish=False)
    converge([a, b])
    a.host.members.discard("user:b")
    a.host.removed.add("user:b")
    a.host.set_meta("user:b", "removed", publish=False)
    a.host.write_text("doc-1", "after removal", publish=False)
    a.host.write_text("doc-big", "R" * (ORGFS_PAGE_BYTES // 2 + 10), publish=False)
    a.host.calls.clear()
    b.host.applied.clear()
    earlier = len(bus.requests("/org/fs3/sync/"))

    for _round in range(3):
        b.sync.sync_from("node-a")
    settle(a, b)

    assert [doc_id for doc_id, _kind, _peer in b.host.applied] == ["meta"]
    assert b.host.docs["meta"].get_state() == a.host.docs["meta"].get_state()
    assert b.host.text("doc-1") == "before"
    assert b.host.text("doc-big") is None
    assert {doc_id for name, doc_id in a.host.calls if name == "read_update"} == {"meta"}
    assert a.sync.blob_store.references(SPACE) == ()
    replies = [json.loads(r) for _q, rs in bus.requests("/org/fs3/sync/")[earlier:] for r in rs]
    assert replies and all(u["docId"] == "meta" for r in replies for u in r["updates"])
    assert all("blob" not in u for r in replies for u in r["updates"])
    _close(a, b)


def test_a_requester_that_never_was_a_member_is_still_refused(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.set_meta("user:a", "rw", publish=False)
    a.host.members.discard("user:b")

    with pytest.raises(SyncError) as raised:
        b.sync.sync_from("node-a")
    assert raised.value.code == "not-a-member"
    assert b.host.applied == []
    assert ("read_update", "meta") not in a.host.calls
    _close(a, b)


def test_requester_refuses_a_supplier_that_fails_the_node_gate(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-1", "x", publish=False)
    b.host.denied_suppliers.add("node-a")

    with pytest.raises(SyncError) as raised:
        b.sync.sync_from("node-a")
    assert raised.value.details.get("reason") == "supplier-not-member"
    assert bus.queries == []
    _close(a, b)


def test_a_reply_with_one_invalid_update_is_refused_whole(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-1", "x", publish=False)

    def corrupt(key: str, body: bytes) -> bytes:
        value = json.loads(body)
        if value.get("type") == "sync-reply" and value["updates"]:
            value["updates"][-1]["update"] = base64.b64encode(b"\xff\x01garbage").decode()
        return json.dumps(value).encode()

    bus.reply_filter = corrupt
    with pytest.raises(SyncError) as raised:
        b.sync.sync_from("node-a")
    assert raised.value.code == "invalid-argument"
    assert b.host.applied == []
    _close(a, b)


def test_rejected_update_is_not_retried_in_a_storm(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-bad", "x", publish=False)
    b.sync.sync_from("node-a")
    b.host.decide = lambda doc, _u, _p: Rejected("protected-path") if doc == "doc-bad" else None

    assert b.sync.schedule_sync_from("node-a")
    time.sleep(0.5)
    attempts = [row for row in b.host.applied if row[0] == "doc-bad"]
    assert 1 <= len(attempts) <= 2
    assert len(bus.query_bytes("/org/fs3/sync/")) <= 3
    _close(a, b)


def test_reply_vectors_are_recorded_as_a_peer_observation(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-1", "x", publish=False)
    b.host.write_text("doc-2", "y")
    converge([a, b])

    [observation] = b.sync.observations()
    assert observation.node == "node-a"
    vectors = a.host.read_vectors(["meta", "tree"])
    assert observation.tree_vector == vectors["tree"]
    assert observation.local_generation == 2
    b.online.discard("node-a")
    assert b.sync.observations() == ()
    _close(a, b)


def test_malformed_request_gets_an_error_reply_and_no_host_reads(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    payload = json.dumps({"schemaVersion": 3}).encode()
    [reply] = bus.sessions["node-a"].query(KeySpace().orgfs3_sync(SPACE, "node-a"), payload)
    assert json.loads(reply.payload)["type"] == "sync-error"
    assert json.loads(reply.payload)["code"] == "invalid-argument"
    assert a.host.calls == []
    a.sync.close()


def test_dependency_that_never_resolves_stops_after_the_retry_budget(bus: Bus) -> None:
    a = make_node(bus, "node-a")
    b = make_node(bus, "node-b")
    a.host.write_text("doc-1", "x", publish=False)
    b.host.decide = lambda doc, _u, _p: Pending("dependency") if doc == "tree" else None

    b.sync.sync_from("node-a")
    settle(a, b)
    rounds = len(bus.query_bytes("/org/fs3/sync/"))
    assert rounds == 1 + SYNC_DEPENDENCY_RETRY_LIMIT
    assert any(event == "orgfs.sync.dependency-unresolved" for _l, event, _f in b.events)
    b.host.decide = None
    b.sync.maintain(time.monotonic())
    b.sync.sync_from("node-a")
    settle(a, b)
    assert same_state(a, b)
    _close(a, b)
