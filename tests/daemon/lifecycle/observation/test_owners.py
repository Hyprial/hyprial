"""Owners publish lifecycle facts without changing their execution policy."""

from types import SimpleNamespace

from hyprial.kernel import LifecycleTarget, HarnessLaunchSpec, ProcessLiveness, ProcessLivenessState
from hyprial.daemon.impl.harnesses.actor.contracts import HarnessProjection, _Record
from hyprial.daemon.impl.lifecycle.observation.ports import LifecycleObservationOwner
from hyprial.daemon.impl.session_actor.internals import _SessionRuntimeState
from hyprial.daemon.impl.application.actors.lifecycle_ops import _LifecycleOpsMixin


TARGET = LifecycleTarget("host", "harness.lifecycle", "agent:o:host:a/incarnation")


def test_harness_projection_publishes_bound_instance_and_survives_aggregate_failure():
    projection = HarnessProjection()
    process = SimpleNamespace(running=True)
    record = _Record(spec=HarnessLaunchSpec(harness="pi", name="a", headless=True), process=process)
    projection.publish({"pi:a": record}, 1)
    owner = projection.lifecycle_observations
    owner.bind(TARGET, "pi:a", record.incarnation)
    assert owner.reader.snapshot(TARGET).state == "running"
    application = SimpleNamespace(_lifecycle_manager=SimpleNamespace(
        is_running=False, crashed=True, last_error="fake aggregate crash",
    ))
    assert _LifecycleOpsMixin._lifecycle_status(application)["crashed"]
    assert owner.reader.snapshot(TARGET).state == "running"
    process.running = False
    process.liveness = lambda: ProcessLiveness(ProcessLivenessState.DEAD, observed=True)
    projection.publish({"pi:a": record}, 2)
    assert owner.reader.snapshot(TARGET).state == "stopped"
    assert owner.reader.observe(TARGET).current.cause == "process-stopped"


def test_replaced_source_cannot_update_old_bound_target():
    owner = LifecycleObservationOwner()
    owner.publish("pi:a", "old", state="running", cause="process-running", available=True)
    owner.bind(TARGET, "pi:a", "old")
    owner.publish("pi:a", "new", state="running", cause="process-running", available=True)
    current = owner.reader.snapshot(TARGET)
    assert current.state == "unknown" and not current.available
    assert current.cause == "binding-changed"


def test_session_contact_is_not_process_liveness_and_drop_is_not_exit():
    runtime = _SessionRuntimeState("daemon-epoch")
    runtime.register("agent:o:host:a", "session-1", 100, confirmed=True)
    owner = runtime.lifecycle_observations
    owner.bind(TARGET, "agent:o:host:a", "session-1")
    assert owner.reader.snapshot(TARGET).state == "unknown"
    assert owner.reader.snapshot(TARGET).cause == "session-contact"
    runtime.drop("agent:o:host:a")
    assert owner.reader.snapshot(TARGET).state == "unknown"
    assert owner.reader.snapshot(TARGET).cause == "session-detached"
    assert not owner.reader.snapshot(TARGET).available


def test_unbind_closes_observer_without_process_command():
    owner = LifecycleObservationOwner()
    owner.publish("pi:a", "one", state="running", cause="process-running", available=True)
    owner.bind(TARGET, "pi:a", "one")
    opened = owner.reader.open(TARGET)
    owner.unbind(TARGET)
    assert owner.reader.observe(TARGET, observer_id=opened.observer_id).status == "closed"
    owner.bind(TARGET, "pi:a", "one")
    assert owner.reader.snapshot(TARGET).state == "running"
