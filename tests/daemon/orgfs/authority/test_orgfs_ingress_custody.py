# Decisions:
# - Phase D, package T2 (triage "pending" for the three directory-ingress
#   tests, decided: keep).  The directory mailbox, its custody bound and the
#   reserved rebuild catch-up belong to ``RuntimeDirectory``, which
#   ``OrgFsRuntime`` composes unchanged.  The overflow test failed on the
#   state runtime only because its announces lacked ``protocol: 3``; it now
#   constructs the state runtime.  The other two pass on both runtimes and
#   keep ``OrgFsRuntime`` (package S points the name at the new runtime).
# - Package S deleted the three ``OrgFsMesh`` tests that exercised the journal
#   mesh directly.  Their
#   behaviour on protocol 3 is covered by
#   ``sync/test_broadcast.py::test_ingress_is_bounded_forged_floods_drop_and_pulls_recover``,
#   ``sync/test_lifecycle.py::test_close_drains_admitted_broadcasts_then_never_calls_the_host``,
#   ``::test_close_raises_when_the_deadline_passes_with_work_in_flight``,
#   ``::test_close_stops_a_round_before_its_next_host_call`` and
#   ``::test_request_blob_queue_is_bounded``.
import threading
import time
import json
from types import SimpleNamespace

from hyprial.daemon.impl.orgfs.composition.state import OrgFsRuntime
from hyprial.daemon.impl.orgfs.composition.vocabulary import _DirectoryEffect
from hyprial.daemon.impl.transport import KeySpace, TransportSample


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def idle(cell):
    snapshot = cell.snapshot()
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
            wait_for(lambda: idle(runtime._directory_cell))
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

    monkeypatch.setattr(runtime_module, "ORGFS_ANNOUNCE_BUFFER_LIMIT", 3)
    entered, release = threading.Event(), threading.Event()
    original_receive = OrgFsRuntime._on_directory_command

    def before_dequeue(runtime, command):
        entered.set()
        assert release.wait(3)
        return original_receive(runtime, command)

    monkeypatch.setattr(OrgFsRuntime, "_on_directory_command", before_dequeue)
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
                json.dumps({"schemaVersion": 1, "type": "orgfs-announce", "protocol": 3, "node": node,
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
    entered, release, scheduled = (
        threading.Event(), threading.Event(), threading.Event()
    )
    original_receive = OrgFsRuntime._on_directory_command

    def before_dequeue(runtime, command):
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original_receive(runtime, command)

    monkeypatch.setattr(OrgFsRuntime, "_on_directory_command", before_dequeue)
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
