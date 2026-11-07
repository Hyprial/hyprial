"""Exercise the live coordinator, not the retired legacy lifecycle runner."""

from types import SimpleNamespace

from hyprial.kernel import LifecycleTarget, HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.actor.contracts import HarnessProjection, _Record
from hyprial.daemon.impl.correlation.correlation import CorrelationEventRouter
from hyprial.daemon.impl.lifecycle.lifecycle_coordinator import LifecycleCoordinator
from hyprial.daemon.impl.lifecycle_manager import LifecyclePorts


def test_recovery_thread_crash_is_dependency_failure_not_child_exit(tmp_path, monkeypatch):
    port = SimpleNamespace()
    coordinator = LifecycleCoordinator(
        tmp_path / "lifecycle.sqlite3", LifecyclePorts(port, port, port, port),
        CorrelationEventRouter(), observation_host_id="test-host",
    )
    projection = HarnessProjection()
    process = SimpleNamespace(running=True)
    record = _Record(HarnessLaunchSpec(harness="pi", name="child", headless=True), process=process)
    projection.publish({"pi:child": record}, 1)
    child = projection.lifecycle_observations
    target = LifecycleTarget("test-host", "harness.lifecycle", "canonical/child-incarnation")
    child.bind(target, "pi:child", record.incarnation)
    reader = coordinator.lifecycle_observations.reader
    before = reader.snapshot(coordinator.lifecycle_target)
    assert before.state == "running"
    subscription = reader.open(coordinator.lifecycle_target)

    def crash():
        raise RuntimeError("injected recovery failure")

    try:
        monkeypatch.setattr(coordinator._store, "pending", crash)
        coordinator._recover_thread.join(2)
        assert not coordinator._recover_thread.is_alive()
        assert coordinator.crashed
        batch = reader.observe(coordinator.lifecycle_target, epoch=before.epoch,
                               after_revision=before.revision)
        assert batch.current.state == "down"
        assert not batch.current.available
        assert [event.transition for event in batch.events] == ["down"]
        assert child.reader.snapshot(target).state == "running"
        assert process.running
    finally:
        assert coordinator.drain(3)
    assert reader.observe(coordinator.lifecycle_target,
                          observer_id=subscription.observer_id).status == "closed"
    assert child.reader.snapshot(target).state == "running"
