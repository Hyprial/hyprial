"""revA reproductions: uncertainty is not exit; quarantine is a read fact."""

import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from hyprial.kernel import HarnessLaunchSpec, LifecycleTarget
from hyprial.daemon.impl.harnesses.streaming.process import SequentialTurnProcess
from hyprial.daemon.impl.harnesses.actor.contracts import HarnessProjection, _Record
from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import LifecycleCoordinator
from hyprial.daemon.impl.lifecycle_manager import LifecyclePorts
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter


@pytest.mark.parametrize("case", ["unknown", "reconnect", "stopping", "verified-stop"])
def test_false_running_requires_positive_exit_evidence(case):
    process = SequentialTurnProcess.__new__(SequentialTurnProcess)
    process._thread = SimpleNamespace(is_alive=lambda: case != "verified-stop")
    process._stopping = threading.Event()
    process._lock = threading.RLock()
    process._client = SimpleNamespace(running=False) if case == "unknown" else None
    process._liveness_probe = None
    process._ready = threading.Event()
    process.last_error = "connection failed, reconnect pending" if case == "reconnect" else None
    process.startup_warnings = ()
    process.label = "test-process"
    if case in {"stopping", "verified-stop"}:
        process._stopping.set()
    assert not process.running
    stopped = case == "verified-stop"
    assert process.liveness().state.value == ("dead" if stopped else "unknown")
    projection = HarnessProjection()
    record = _Record(HarnessLaunchSpec(harness="pi", name="fake", headless=True), process=process)
    projection.publish({"pi:fake": record}, 1)
    owner = projection.lifecycle_observations
    target = LifecycleTarget("test-host", "harness.lifecycle", "canonical/fake")
    owner.bind(target, "pi:fake", record.incarnation)
    try:
        current = owner.reader.snapshot(target)
        assert current.state == ("stopped" if stopped else "unknown")
        assert current.available is stopped
        assert current.cause == ("process-stopped" if stopped else "process-unavailable")
        assert not projection.status()[0]["running"]  # Existing boolean stays unchanged.
    finally:
        owner.close()


@pytest.mark.parametrize("read_method", ["snapshot", "observe", "open"])
def test_quarantine_is_visible_without_crashed_read(tmp_path, read_method):
    port = SimpleNamespace()
    coordinator = LifecycleCoordinator(
        tmp_path / "lifecycle.sqlite3", LifecyclePorts(port, port, port, port),
        CorrelationEventRouter(), observation_host_id="test-host",
    )
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    projection = HarnessProjection()
    record = _Record(HarnessLaunchSpec(harness="pi", name="child", headless=True),
                     process=SimpleNamespace(running=child.poll() is None))
    projection.publish({"pi:child": record}, 1)
    target = LifecycleTarget("test-host", "harness.lifecycle", "canonical/child")
    projection.lifecycle_observations.bind(target, "pi:child", record.incarnation)
    try:
        reader = coordinator.lifecycle_observations.reader
        before = reader.snapshot(coordinator.lifecycle_target)
        for index in range(4):
            deadline = time.monotonic() + 3
            while coordinator._runtime.snapshot(coordinator._handle).state.value != "running":
                assert time.monotonic() < deadline
                time.sleep(.01)
            coordinator._runtime.tell(coordinator._handle, object())
            deadline = time.monotonic() + 3
            while coordinator._runtime.snapshot(coordinator._handle).failures_in_window < index + 1:
                assert time.monotonic() < deadline
                time.sleep(.01)
        deadline = time.monotonic() + 3
        while coordinator._runtime.snapshot(coordinator._handle).state.value != "quarantined":
            assert time.monotonic() < deadline
            time.sleep(.01)
        # Never read coordinator.crashed: the monitor must be sufficient by itself.
        if read_method == "snapshot":
            current = reader.snapshot(coordinator.lifecycle_target)
        elif read_method == "open":
            opened = reader.open(coordinator.lifecycle_target)
            current = opened.initial.current
        else:
            batch = reader.observe(coordinator.lifecycle_target, epoch=before.epoch,
                                   after_revision=before.revision)
            current = batch.current
            assert [event.transition for event in batch.events] == ["down"]
        assert current.state == "down"
        assert not current.available
        assert projection.lifecycle_observations.reader.snapshot(target).state == "running"
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=3)
        projection.lifecycle_observations.close()
        assert coordinator.drain(3)
