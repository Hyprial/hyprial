import threading
import time
import json
from types import SimpleNamespace

from hyprial.daemon.impl.orgfs.mesh import OrgFsMesh
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.composition.vocabulary import _DirectoryEffect
from hyprial.daemon.impl.transport import KeySpace, TransportSample, ZenohConfig
from hyprial.daemon.impl.transport.session_actor import TransportSessionAuthority


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def idle(runtime, handle):
    snapshot = runtime.snapshot(handle)
    return snapshot.queued == snapshot.in_flight == 0


def test_directory_bounds_total_custody_and_drains_deferred_effect(tmp_path):
    runtime = OrgFsRuntime(tmp_path, node_id="bounded", author="user:owner")
    runtime._directory_capacity = 2
    runtime._start_directory_ingress()
    runtime._directory_effects._capacity = 1
    entered, release = threading.Event(), threading.Event()
    calls = []

    def execute(batch):
        calls.append(batch)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)

    runtime._execute_directory_effect = execute
    runtime._apply_announcement_command = lambda sample: [_DirectoryEffect("announce")]
    try:
        runtime._admit_directory_sample("announce", TransportSample("first", b""))
        assert entered.wait(1)
        runtime._admit_directory_sample("announce", TransportSample("second", b""))
        wait_for(lambda: len(runtime._directory_deferred) == 1)
        for index in range(8):
            runtime._admit_directory_sample(
                "announce", TransportSample(str(index), b"")
            )
            wait_for(lambda: idle(runtime._directory_runtime, runtime._directory_actor))
        assert len(runtime._directory_deferred) == 1
        assert runtime.directory_ingress_pending == 2
        assert runtime.directory_ingress_dropped == 8
        assert not runtime._close_directory_ingress(0.01)
        assert runtime.directory_ingress_pending == 2
    finally:
        release.set()
        runtime.close()
    assert len(calls) == 2
    assert runtime.directory_ingress_pending == 0


def test_directory_mailbox_preserves_the_reserved_overflow_envelope(tmp_path, monkeypatch):
    import hyprial.daemon.impl.orgfs.composition.directory as runtime_module
    from hyprial.kernel.impl.actor_runtime.backends import pykka as pykka_backend

    monkeypatch.setattr(runtime_module, "ORGFS_ANNOUNCE_BUFFER_LIMIT", 3)
    entered, release = threading.Event(), threading.Event()
    original_receive = pykka_backend._RuntimeActor.on_receive

    def before_dequeue(actor, message):
        if actor._handle.name == "orgfs-directory-ingress":
            entered.set()
            assert release.wait(3)
        return original_receive(actor, message)

    monkeypatch.setattr(pykka_backend._RuntimeActor, "on_receive", before_dequeue)
    events = []
    runtime = OrgFsRuntime(
        tmp_path, node_id="reader", author="user:reader",
        logger=lambda level, event, **fields: events.append((event, fields)),
    )
    runtime._supplier_online = lambda _node: False
    runtime._start_directory_ingress()
    try:
        for index in range(4):
            node = f"node-{index}"
            runtime._admit_directory_sample("announce", TransportSample(
                KeySpace().orgfs_announce(node),
                json.dumps({"schemaVersion": 1, "type": "orgfs-announce", "node": node,
                            "spaces": [{"spaceId": f"space-{index}"}]}).encode(),
            ))
            if index == 0:
                assert entered.wait(1)
        assert runtime.directory_ingress_pending == 4
        assert runtime.directory_ingress_dropped == 0
        release.set()
        wait_for(lambda: runtime.directory_ingress_pending == 0)
        assert len(runtime._pending_announces) == 3
        assert "node-0" not in runtime._pending_announces
        assert any(event == "orgfs.announce.buffer-dropped" and fields["droppedCount"] == 1
                   for event, fields in events)
    finally:
        release.set()
        runtime.close()


def test_full_directory_mailbox_retains_rebuild_catchup(tmp_path, monkeypatch):
    from hyprial.kernel.impl.actor_runtime.backends import pykka as pykka_backend

    entered, release, scheduled = (
        threading.Event(), threading.Event(), threading.Event()
    )
    original_receive = pykka_backend._RuntimeActor.on_receive

    def before_dequeue(actor, message):
        if actor._handle.name == "orgfs-directory-ingress" and not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original_receive(actor, message)

    monkeypatch.setattr(pykka_backend._RuntimeActor, "on_receive", before_dequeue)
    runtime = OrgFsRuntime(
        tmp_path, node_id="reader", author="user:reader"
    )
    runtime._directory_capacity = 2
    runtime._start_directory_ingress()
    runtime._apply_announcement_command = lambda _sample: []
    runtime._post_rebuild_effects = lambda: [
        _DirectoryEffect(
            "post-rebuild-sync", peer="node-owner", space_ids=("space",)
        )
    ]
    runtime._mesh = lambda _space_id: SimpleNamespace(
        schedule_sync_from=lambda _peer: scheduled.set() or True
    )
    try:
        runtime._admit_directory_sample("announce", TransportSample("first", b""))
        assert entered.wait(1)
        runtime._admit_directory_sample("announce", TransportSample("second", b""))
        assert runtime.directory_ingress_pending == 2

        runtime._admit_directory_sample(
            "rebuild-complete", TransportSample("transport/rebuild", b"")
        )
        release.set()

        assert scheduled.wait(2)
    finally:
        release.set()
        runtime.close()


class Native:
    config = ZenohConfig()

    def __init__(self):
        self.callbacks = []

    def subscribe(self, key, callback):
        self.callbacks.append(callback)
        return SimpleNamespace(close=lambda: None)

    def declare_query_handler(self, key, handler):
        return SimpleNamespace(close=lambda: None)

    def close(self):
        pass


def test_wrapped_transport_uses_bounded_mesh_ingress_and_close_keeps_custody():
    native = Native()
    transport = TransportSessionAuthority(native)
    mesh = OrgFsMesh(
        transport, SimpleNamespace(space_id="space"), "node", author="user:owner"
    )
    mesh._ingress_capacity = 2
    mesh._ingress_effects._capacity = 1
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    calls, errors = [], []

    def process(sample):
        calls.append(sample.payload)
        if sample.payload == b"first":
            entered.set()
            assert release.wait(3)

    mesh._process_log_sample = process
    callback = native.callbacks[0]
    closer = None
    try:
        callback(TransportSample("key", b"first"))
        assert entered.wait(1)
        assert mesh.ingress_pending == 1
        callback(TransportSample("key", b"second"))
        wait_for(lambda: len(mesh._ingress_deferred) == 1)
        for index in range(8):
            callback(TransportSample("key", str(index).encode()))
            wait_for(lambda: transport.callback_pool_snapshot().outstanding == 0)
        assert len(mesh._ingress_deferred) == 1
        assert mesh.ingress_pending == 2
        assert mesh.ingress_dropped == 8

        def close():
            try:
                mesh.close()
            except BaseException as error:
                errors.append(error)
            finally:
                closed.set()

        closer = threading.Thread(target=close)
        closer.start()
        assert not closed.wait(0.02)
        release.set()
        assert closed.wait(2)
        assert errors == []
        assert mesh.ingress_pending == 0
    finally:
        release.set()
        if closer is not None:
            closer.join(2)
        mesh.close()
        transport.close()
    assert calls == [b"first", b"second"]


def test_mesh_close_budget_retains_active_sync_and_retry_does_not_repeat_it():
    import pytest

    transport = TransportSessionAuthority(Native())
    mesh = OrgFsMesh(
        transport, SimpleNamespace(space_id="space"), "node", author="user:owner"
    )
    entered, release = threading.Event(), threading.Event()
    calls = []

    def sync(peer, *, timeout, deadline_monotonic):
        assert timeout <= 2.0
        assert deadline_monotonic > time.monotonic()
        calls.append(peer)
        entered.set()
        assert release.wait(3)

    mesh.sync_from = sync
    try:
        assert mesh.schedule_sync_from("peer")
        assert entered.wait(1)
        with pytest.raises(TimeoutError):
            mesh.close(timeout=0.05)
        assert not mesh._ingress_closed
        assert not mesh.schedule_sync_from("after-close")
        release.set()
        mesh.close(timeout=2)
        assert mesh._ingress_closed
        assert calls == ["peer"]
    finally:
        release.set()
        mesh.close(timeout=2)
        transport.close()


def test_blob_queue_limit_is_total_even_for_existing_digests(monkeypatch):
    monkeypatch.setattr('hyprial.daemon.impl.orgfs.network.recovery.ORGFS_BLOB_FETCH_QUEUE_LIMIT', 3)
    transport = TransportSessionAuthority(Native())
    mesh = OrgFsMesh(
        transport, SimpleNamespace(space_id="space"), "node", author="user:owner"
    )
    entered, release = threading.Event(), threading.Event()

    def recover(digest):
        entered.set()
        assert release.wait(3)

    mesh._run_blob_recovery = recover
    try:
        mesh._defer_blob_recovery(b"a1", supplier="peer", digest="a" * 64)
        assert entered.wait(1)
        mesh._defer_blob_recovery(b"b1", supplier="peer", digest="b" * 64)
        mesh._defer_blob_recovery(b"a2", supplier="peer", digest="a" * 64)
        mesh._defer_blob_recovery(b"b2", supplier="peer", digest="b" * 64)
        assert mesh._blob_fetch_pending_count == 3
        assert mesh._blob_fetch_dropped == 1
    finally:
        release.set()
        mesh.close()
        transport.close()
