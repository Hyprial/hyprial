"""Catch-up must survive either ordering of two independent actor mailboxes."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import KeySpace, LivelinessDirectory, TransportSample
from hyprial.daemon.impl.transport.presence_actor import PresenceAuthority
from hyprial.kernel.impl.actor_runtime.effects import EffectCompleted


class ControlledSession:
    """No network timing; keep the real presence and OrgFS actor owners."""

    def __init__(self):
        self.observers = []

    def on_rebuild(self, callback):
        return lambda: None

    def observe_liveliness(self, key, callback, *, history=True):
        self.observers.append((key, callback))
        return SimpleNamespace(close=lambda: self.observers.remove((key, callback)))

    def subscribe(self, key, callback):
        return SimpleNamespace(close=lambda: None)

    declare_query_handler = subscribe

    def put(self, key, payload):
        pass

    def get_liveliness(self, key, *, timeout=1.0):
        return []


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.001)


@pytest.mark.parametrize("query_result", ["confirmed", "empty", "error"])
def test_query_confirmed_delete_notifies_orgfs_and_preserves_inbox_slot(
    tmp_path, monkeypatch, query_result
):
    """Only the query receipt, without another PUT, can cause the second sync."""
    query_entered, query_release, receipt_settled = (
        threading.Event() for _ in range(3)
    )
    synced = threading.Event()
    queries, syncs, inbox_wakes = [], [], []
    original_receive = PresenceAuthority._receive

    def receive(self, command):
        original_receive(self, command)
        if isinstance(command, EffectCompleted):
            receipt_settled.set()

    monkeypatch.setattr(PresenceAuthority, "_receive", receive)
    session = ControlledSession()

    def query(key, *, timeout):
        queries.append(key)
        query_entered.set()
        assert query_release.wait(3), "query control not released"
        if query_result == "error":
            raise OSError("controlled query error")
        return [TransportSample(key, b"")] if query_result == "confirmed" else []

    monkeypatch.setattr(session, "get_liveliness", query)
    directory = LivelinessDirectory(session)
    runtime = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader")
    try:
        space = runtime.facade.create_space("query confirmed supplier")
        inbox_observer = inbox_wakes.append
        directory.bind_actor_online(inbox_observer)
        runtime.bind_transport(
            session, supplier_online=directory.actor_online,
            observe_supplier_online=directory.observe_actor_online,
        )
        mesh = runtime._meshes[space.space_id]

        def sync(peer, **_kwargs):
            syncs.append(peer)
            synced.set()

        monkeypatch.setattr(mesh, "sync_from", sync)
        key = KeySpace().actor_liveliness("node-a")
        directory._actor_event(TransportSample(key, b""))
        assert synced.wait(3), "normal PUT did not notify OrgFS"
        wait_until(lambda: runtime.directory_ingress_pending == 0 and not mesh._sync_pending)
        assert syncs == ["node-a"]
        assert [wake.actor for wake in inbox_wakes] == ["node-a"]
        synced.clear()

        directory._actor_event(TransportSample(key, b"", kind="delete"))
        assert query_entered.wait(3)
        # Query is held: no new PUT, no replay registration, no raw OrgFS wake.
        assert directory.actor_online("node-a")
        assert syncs == ["node-a"]
        assert len(inbox_wakes) == 1
        query_release.set()
        assert receipt_settled.wait(3)
        assert queries == [key]
        if query_result == "confirmed":
            assert len(inbox_wakes) == 2, "confirmed DELETE lost inbox wake"
            assert synced.wait(1), "confirmed DELETE did not notify OrgFS"
            wait_until(lambda: runtime.directory_ingress_pending == 0 and not mesh._sync_pending)
            assert syncs == ["node-a", "node-a"]
            assert [wake.sequence for wake in inbox_wakes] == [1, 2]
            assert directory.actor_online("node-a")
        else:
            assert not directory.actor_online("node-a")
            assert len(inbox_wakes) == 1
            assert not synced.is_set()
            assert syncs == ["node-a"]
        runtime.close()
        assert directory._authority._online_subscribers == {}
        assert directory._authority._online_observer is inbox_observer
        if query_result == "confirmed":
            # After OrgFS unregisters, a further confirmation still reaches the
            # inbox slot but cannot resurrect meshes or directory work.
            receipt_settled.clear()
            directory._actor_event(TransportSample(key, b"", kind="delete"))
            assert receipt_settled.wait(3)
            assert len(inbox_wakes) == 3
            assert queries == [key, key]
            assert syncs == ["node-a", "node-a"]
            assert not runtime._meshes
            assert runtime.directory_ingress_pending == 0
    finally:
        query_release.set()
        runtime.close()
        directory.close()


@pytest.mark.parametrize("presence_first", [True, False], ids=["settled-first", "settled-last"])
def test_catchup_survives_presence_settlement_order(tmp_path, monkeypatch, presence_first):
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    entered, release, settled = (threading.Event() for _ in range(3))
    sync_called, worker_finished, raw_processed = (threading.Event() for _ in range(3))
    calls = []
    original_receive = PresenceAuthority._receive

    def controlled_receive(self, command):
        if command.identity == "node-a":
            entered.set()
            assert release.wait(10), "presence control was not released"
        original_receive(self, command)
        settled.set()

    monkeypatch.setattr(PresenceAuthority, "_receive", controlled_receive)
    session = ControlledSession()
    directory = LivelinessDirectory(session)
    runtime = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader")
    try:
        space = runtime.facade.create_space("restart")
        runtime.bind_transport(
            session, supplier_online=directory.actor_online,
            observe_supplier_online=directory.observe_actor_online,
        )
        mesh = runtime._meshes[space.space_id]
        original_worker = mesh._run_scheduled_sync
        original_liveliness = runtime._apply_liveliness_command

        def observed_liveliness(sample):
            try:
                return original_liveliness(sample)
            finally:
                raw_processed.set()

        monkeypatch.setattr(runtime, "_apply_liveliness_command", observed_liveliness)

        def observed_worker(peer):
            try:
                original_worker(peer)
            finally:
                worker_finished.set()

        def observed_sync(peer, **_kwargs):
            calls.append(peer)
            sync_called.set()

        monkeypatch.setattr(mesh, "_run_scheduled_sync", observed_worker)
        monkeypatch.setattr(mesh, "sync_from", observed_sync)
        sample = TransportSample(KeySpace().actor_liveliness("node-a"), b"")
        directory._actor_event(sample)
        assert entered.wait(3)
        if presence_first:
            release.set()
            assert settled.wait(3)
            assert directory.actor_online("node-a")
        runtime._admit_directory_sample("liveliness", sample)
        assert raw_processed.wait(3)
        wait_until(lambda: runtime.directory_ingress_pending == 0)
        if not presence_first:
            assert not directory.actor_online("node-a")
            wait_until(lambda: not mesh._sync_pending)
            assert not sync_called.is_set()
            assert not mesh._sync_pending
            release.set()
            assert settled.wait(3)
            assert directory.actor_online("node-a")
        assert sync_called.wait(1), "committed presence did not recover the dropped catch-up"
        assert worker_finished.wait(3)
        assert calls == ["node-a"]
    finally:
        release.set()
        runtime.close()
        directory.close()


def test_committed_supplier_delete_still_fences_queued_sync(tmp_path, monkeypatch):
    session = ControlledSession()
    directory = LivelinessDirectory(session)
    runtime = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader")
    entered, release, finished = (threading.Event() for _ in range(3))
    calls = []
    try:
        space = runtime.facade.create_space("delete fence")
        runtime.bind_transport(
            session, supplier_online=directory.actor_online,
            observe_supplier_online=directory.observe_actor_online,
        )
        mesh = runtime._meshes[space.space_id]
        original_worker = mesh._run_scheduled_sync

        def held_worker(peer):
            entered.set()
            try:
                assert release.wait(10)
                original_worker(peer)
            finally:
                finished.set()

        monkeypatch.setattr(mesh, "_run_scheduled_sync", held_worker)
        monkeypatch.setattr(mesh, "sync_from", lambda peer, **_kw: calls.append(peer))
        key = KeySpace().actor_liveliness("node-a")
        directory._actor_event(TransportSample(key, b""))
        assert entered.wait(3)
        directory._actor_event(TransportSample(key, b"", kind="delete"))
        wait_until(lambda: not directory.actor_online("node-a"))
        release.set()
        assert finished.wait(3)
        assert calls == []
        assert not mesh._sync_pending
        runtime.close()
        assert directory._authority._online_subscribers == {}
        # A snapshotted callback that races unsubscribe still hits closed
        # directory admission; it cannot resurrect the runtime or mesh.
        from hyprial.daemon.impl.transport.presence_actor import ActorOnlineTransition

        runtime._supplier_became_online(ActorOnlineTransition(1, "node-a", 2))
        assert not runtime._meshes
        assert runtime.directory_ingress_pending == 0
    finally:
        release.set()
        runtime.close()
        directory.close()


def test_late_binding_replays_supplier_without_replacing_inbox_wake(tmp_path, monkeypatch):
    session = ControlledSession()
    directory = LivelinessDirectory(session)
    runtime = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader")
    observed = threading.Event()
    inbox_wakes = []
    try:
        directory._actor_event(TransportSample(KeySpace().actor_liveliness("node-a"), b""))
        wait_until(lambda: directory.actor_online("node-a"))
        runtime.facade.create_space("replayed supplier")
        from hyprial.daemon.impl.orgfs.mesh import OrgFsMesh

        monkeypatch.setattr(OrgFsMesh, "sync_from", lambda *_a, **_kw: observed.set())
        runtime.bind_transport(
            session, supplier_online=directory.actor_online,
            observe_supplier_online=directory.observe_actor_online,
        )
        assert observed.wait(3)
        directory.bind_actor_online(inbox_wakes.append)
        assert [event.actor for event in inbox_wakes] == ["node-a"]
        assert len(directory._authority._online_subscribers) == 1
        runtime.close()
        assert directory._authority._online_subscribers == {}
        directory._actor_event(TransportSample(KeySpace().actor_liveliness("node-c"), b""))
        wait_until(lambda: len(inbox_wakes) == 2)
        assert [event.actor for event in inbox_wakes] == ["node-a", "node-c"]
    finally:
        runtime.close()
        directory.close()


def test_non_host_replay_cannot_consume_directory_credit_or_drop_announce(tmp_path, monkeypatch):
    session = ControlledSession()
    directory = LivelinessDirectory(session)
    runtime = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader")
    entered, release, applied = (threading.Event() for _ in range(3))
    original_command = runtime._on_directory_command
    original_apply = runtime._apply_announce

    def blocked_command(command):
        if getattr(command, "sample", None) == TransportSample("blocker", b"{}"):
            entered.set()
            assert release.wait(10), "replay control was not released"
        original_command(command)

    def observed_apply(peer, announcement):
        newly_held = original_apply(peer, announcement)
        applied.set()
        return newly_held

    monkeypatch.setattr(runtime, "_on_directory_command", blocked_command)
    monkeypatch.setattr(runtime, "_apply_announce", observed_apply)
    try:
        space = runtime.facade.create_space("replay pressure")
        count = runtime._directory_capacity + 64
        for index in range(count):
            actor = f"agent:owner@example.com:node-a:worker-{index}"
            directory._actor_event(TransportSample(KeySpace().actor_liveliness(actor), b""))
        directory._actor_event(TransportSample(KeySpace().actor_liveliness("node-a"), b""))
        wait_until(lambda: len(directory.online_actors()) == count + 1)
        runtime._start_directory_ingress()
        runtime._admit_directory_sample("announce", TransportSample("blocker", b"{}"))
        assert entered.wait(3)
        runtime.bind_transport(
            session, supplier_online=directory.actor_online,
            observe_supplier_online=directory.observe_actor_online,
        )
        mesh = runtime._meshes[space.space_id]
        monkeypatch.setattr(mesh, "sync_from", lambda *_a, **_kw: None)
        announcement = {
            "schemaVersion": 1, "type": "orgfs-announce", "node": "node-a",
            "spaces": [{"spaceId": space.space_id, "roles": ["member"], "mode": "rw"}],
        }
        runtime._admit_directory_sample("announce", TransportSample(
            KeySpace().orgfs_announce("node-a"), json.dumps(announcement).encode(),
        ))
        # Consumer remains blocked: only blocker + host wake + actual announce
        # may hold credit. No scheduling luck can drain the non-host replay.
        assert runtime.directory_ingress_dropped == 0
        assert runtime.directory_ingress_pending == 3
        release.set()
        assert applied.wait(3)
        wait_until(lambda: runtime.directory_ingress_pending == 0)
        assert runtime._holder_snapshot(space.space_id) == ("node-a",)
        assert runtime.directory_ingress_dropped == 0
    finally:
        release.set()
        runtime.close()
        directory.close()
