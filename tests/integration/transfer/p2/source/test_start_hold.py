"""TC013/014 prerequisites: hold and starts share one admission authority."""

import threading

import pytest

from hyprial.daemon.impl.harnesses.runtime.ports import HarnessStartTimerElapsedCommand
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.actor.facade import HarnessRuntimeFacade
from hyprial.kernel import PortCommandRejected

from .hold_support import Events, Identity, Launcher, Process, ensure, hold, spec


def release(actor, *, correlation="release", token="source-incarnation",
            operation="move", generation=1, name="source"):
    from hyprial.daemon.impl.harnesses.runtime import ports
    command = getattr(ports, "ReleaseStartsHold", None)
    assert command is not None, "missing incarnation-bound transfer hold release"
    actor.submit(command(correlation, actor=name, expected_entity_token=token,
                         operation_id=operation, generation=generation))


@pytest.fixture
def harness_actor():
    created = []

    def build(*, blocked=False):
        launcher, identity = Launcher(blocked=blocked), Identity()
        runtime = HarnessRuntimeFacade(HarnessRuntimeActor(
            launcher, agent_identity=identity, start_timeout_seconds=0,
        ))
        events = Events(runtime._actor)
        created.append((runtime, launcher))
        return runtime, launcher, identity, events

    yield build
    for runtime, launcher in created:
        launcher.release.set()
        runtime.stop(5)


def test_hold_rejects_new_start_without_calling_launcher(harness_actor):
    runtime, launcher, _, events = harness_actor()
    hold(runtime._actor)
    ack = events.wait("hold")
    assert ack.disposition == "held"
    ensure(runtime._actor)
    refusal = events.wait("start")
    assert isinstance(refusal, PortCommandRejected)
    assert refusal.code == "TRANSFER_STARTS_HELD"
    assert not launcher.processes


def test_canonical_actor_hold_rejects_local_name_start(harness_actor):
    from hyprial.daemon.impl.harnesses.runtime.ports import HoldStartsForTransfer
    runtime, launcher, _, events = harness_actor()
    canonical = 'agent:owner:node:source'
    runtime._actor.submit(HoldStartsForTransfer(
        'hold', actor=canonical, expected_entity_token='source-incarnation',
        operation_id='move', generation=1,
    ))
    ack = events.wait('hold')
    assert ack.disposition == 'held' and ack.actor == canonical
    ensure(runtime._actor)
    assert getattr(events.wait('start'), 'code', None) == 'TRANSFER_STARTS_HELD'
    assert not launcher.processes


def test_canonical_hold_waits_for_local_name_inflight_start(harness_actor):
    from hyprial.daemon.impl.harnesses.runtime.ports import HoldStartsForTransfer
    runtime, launcher, _, events = harness_actor(blocked=True)
    ensure(runtime._actor)
    assert launcher.entered.wait(5)
    runtime._actor.submit(HoldStartsForTransfer(
        'hold', actor='agent:owner:node:source', expected_entity_token='source-incarnation',
        operation_id='move', generation=1,
    ))
    events.barrier(runtime._actor)
    assert not any(e.correlation_id == 'hold' for e in events.items)
    launcher.release.set()
    assert events.wait('hold').drained == ('start',)


def test_stale_hold_does_not_suppress_replacement(harness_actor):
    runtime, _, identity, events = harness_actor()
    identity.token = "replacement"
    hold(runtime._actor)
    assert events.wait("hold").disposition == "entity-mismatch"
    assert runtime.start(spec())


def test_token_is_rechecked_on_later_start(harness_actor):
    runtime, _, identity, events = harness_actor()
    hold(runtime._actor)
    assert events.wait("hold").disposition == "held"
    identity.token = "replacement"
    assert runtime.start(spec()), "old incarnation hold suppressed a new incarnation"


def test_hold_waits_for_admitted_launch_and_keeps_mailbox_readable(harness_actor):
    runtime, launcher, _, events = harness_actor(blocked=True)
    ensure(runtime._actor)
    assert launcher.entered.wait(5)
    hold(runtime._actor)
    events.barrier(runtime._actor)
    assert not any(e.correlation_id == "hold" for e in events.items)
    launcher.release.set()
    events.wait("start")
    ack = events.wait("hold")
    assert ack.disposition == "held"
    assert ack.drained == ("start",)
    assert runtime.status()[0]["running"]


def test_start_timeout_does_not_erase_inflight_custody(harness_actor):
    runtime, launcher, _, events = harness_actor(blocked=True)
    ensure(runtime._actor)
    assert launcher.entered.wait(5)
    hold(runtime._actor)
    events.barrier(runtime._actor)
    record = runtime._actor._records["fake:source"]
    runtime._actor.submit(HarnessStartTimerElapsedCommand(
        "start", record.generation, 0, "fake:source"
    ))
    assert isinstance(events.wait("start"), PortCommandRejected)
    assert not any(e.correlation_id == "hold" for e in events.items)
    launcher.release.set()
    ack = events.wait("hold")
    assert ack.disposition == "held"
    assert ack.drained == ("start",)
    assert not launcher.processes[0].running, "late process escaped hold draining"


def test_held_restore_is_deferred_without_blocking_healthy_worker(harness_actor):
    runtime, launcher, _, events = harness_actor()
    hold(runtime._actor)
    assert events.wait("hold").disposition == "held"
    result = runtime.restore((spec(), spec("healthy")))
    assert result.deferred == 1
    assert result.restored == 1
    assert len(launcher.processes) == 1


def test_hold_prevents_automatic_reconcile_start(harness_actor):
    runtime, launcher, _, events = harness_actor()
    runtime.start(spec())
    hold(runtime._actor)
    assert events.wait("hold").disposition == "held"
    launcher.processes[0].running = False
    runtime.reconcile()
    events.barrier(runtime._actor)
    assert len(launcher.processes) == 1


def test_identity_query_failure_cannot_retire_hold(harness_actor, monkeypatch):
    runtime, launcher, identity, events = harness_actor()
    hold(runtime._actor)
    assert events.wait("hold").disposition == "held"

    def unavailable(actor):
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(identity, "entity_token", unavailable)
    ensure(runtime._actor)
    result = events.wait("start")
    assert isinstance(result, PortCommandRejected)
    assert result.code == "TRANSFER_CUSTODY_UNAVAILABLE"
    assert not launcher.processes


def test_hold_waits_until_late_process_stop_completes(harness_actor, monkeypatch):
    runtime, launcher, _, events = harness_actor(blocked=True)
    stop_entered, stop_release = threading.Event(), threading.Event()

    def stop(process):
        stop_entered.set()
        assert stop_release.wait(5)
        process.running = False

    monkeypatch.setattr(Process, "stop", stop)
    try:
        ensure(runtime._actor)
        assert launcher.entered.wait(5)
        hold(runtime._actor)
        events.barrier(runtime._actor)
        record = runtime._actor._records["fake:source"]
        runtime._actor.submit(HarnessStartTimerElapsedCommand(
            "start", record.generation, 0, "fake:source"
        ))
        events.wait("start")
        launcher.release.set()
        assert stop_entered.wait(5)
        assert not any(e.correlation_id == "hold" for e in events.items)
        stop_release.set()
        assert events.wait("hold").disposition == "held"
    finally:
        stop_release.set()


def test_same_operation_attaches_but_different_operation_cannot_take_hold(harness_actor):
    runtime, launcher, _, events = harness_actor(blocked=True)
    ensure(runtime._actor)
    assert launcher.entered.wait(5)
    hold(runtime._actor)
    hold(runtime._actor, correlation="attached")
    hold(runtime._actor, correlation="conflict", operation="other")
    assert events.wait("conflict").code == "TRANSFER_CONFLICT"
    launcher.release.set()
    assert events.wait("hold").disposition == "held"
    assert events.wait("attached").drained == ("start",)
    hold(runtime._actor, correlation="replayed")
    assert events.wait("replayed").drained == ("start",)


def test_ack_rechecks_incarnation_after_inflight_launch(harness_actor):
    runtime, launcher, identity, events = harness_actor(blocked=True)
    ensure(runtime._actor)
    assert launcher.entered.wait(5)
    hold(runtime._actor)
    events.barrier(runtime._actor)
    identity.token = "replacement"
    launcher.release.set()
    assert events.wait("hold").disposition == "entity-mismatch"


def test_exact_release_allows_start_without_destroying_worker(harness_actor):
    runtime, launcher, _, events = harness_actor()
    hold(runtime._actor)
    assert events.wait("hold").disposition == "held"
    release(runtime._actor)
    assert events.wait("release").disposition == "released"
    assert runtime.start(spec())
    assert len(launcher.processes) == 1


@pytest.mark.parametrize("change", [
    {"token": "other"}, {"operation": "other"}, {"generation": 2},
    {"name": "other"},
])
def test_release_mismatch_preserves_active_hold(harness_actor, change):
    runtime, launcher, _, events = harness_actor()
    hold(runtime._actor)
    assert events.wait("hold").disposition == "held"
    release(runtime._actor, **change)
    assert isinstance(events.wait("release"), PortCommandRejected)
    ensure(runtime._actor)
    assert events.wait("start").code == "TRANSFER_STARTS_HELD"
    assert not launcher.processes


def test_old_release_cannot_touch_replacement_hold(harness_actor):
    runtime, launcher, identity, events = harness_actor()
    hold(runtime._actor)
    events.wait("hold")
    identity.token = "replacement"
    hold(runtime._actor, correlation="new-hold", token="replacement", operation="new")
    assert events.wait("new-hold").disposition == "held"
    release(runtime._actor)
    assert isinstance(events.wait("release"), PortCommandRejected)
    ensure(runtime._actor)
    assert events.wait("start").code == "TRANSFER_STARTS_HELD"
    assert not launcher.processes


def test_release_identity_unavailable_preserves_hold(harness_actor, monkeypatch):
    runtime, _, identity, events = harness_actor()
    hold(runtime._actor)
    events.wait("hold")
    monkeypatch.setattr(identity, "entity_token", lambda actor: None)
    release(runtime._actor)
    assert events.wait("release").code == "TRANSFER_CUSTODY_UNAVAILABLE"
    monkeypatch.setattr(identity, "entity_token", lambda actor: "source-incarnation")
    ensure(runtime._actor)
    assert events.wait("start").code == "TRANSFER_STARTS_HELD"


def test_release_does_not_abandon_unfinished_drain(harness_actor):
    runtime, launcher, _, events = harness_actor(blocked=True)
    ensure(runtime._actor)
    assert launcher.entered.wait(5)
    hold(runtime._actor)
    release(runtime._actor)
    assert events.wait("release").code == "TRANSFER_CONFLICT"
    launcher.release.set()
    assert events.wait("hold").disposition == "held"
    release(runtime._actor, correlation="after-drain")
    assert events.wait("after-drain").disposition == "released"


def test_missing_hold_is_not_successful_release(harness_actor):
    runtime, _, _, events = harness_actor()
    release(runtime._actor)
    assert events.wait("release").code == "TRANSFER_CONFLICT"


def test_release_rechecks_current_incarnation_without_retiring_hold(harness_actor):
    runtime, _, identity, events = harness_actor()
    hold(runtime._actor)
    events.wait("hold")
    identity.token = "replacement"
    release(runtime._actor)
    assert events.wait("release").code == "TRANSFER_CONFLICT"
    # Unlike a fresh start resolving a new incarnation, stale release is not
    # authority to retire any hold, including the original one.
    identity.token = "source-incarnation"
    ensure(runtime._actor)
    assert events.wait("start").code == "TRANSFER_STARTS_HELD"


def test_current_token_cannot_release_previous_incarnation_hold(harness_actor):
    runtime, _, identity, events = harness_actor()
    hold(runtime._actor)
    events.wait("hold")
    identity.token = "replacement"
    release(runtime._actor, token="replacement")
    assert events.wait("release").code == "TRANSFER_CONFLICT"
    identity.token = "source-incarnation"
    ensure(runtime._actor)
    assert events.wait("start").code == "TRANSFER_STARTS_HELD"


@pytest.mark.parametrize("change", [
    {"generation": True}, {"generation": 0}, {"token": " "},
    {"operation": " "}, {"name": " "},
])
def test_release_invalid_input_is_typed_and_preserves_hold(harness_actor, change):
    runtime, _, _, events = harness_actor()
    hold(runtime._actor)
    events.wait("hold")
    release(runtime._actor, **change)
    assert events.wait("release").code == "INVALID_ARGUMENT"
    ensure(runtime._actor)
    assert events.wait("start").code == "TRANSFER_STARTS_HELD"


def test_hold_acknowledgements_carry_the_live_projection_version(harness_actor):
    """Every hold/release acknowledgment is version-fenced like other events.

    The domain-port contract requires async events to carry
    (correlation_id, generation, version); this pins that the two transfer
    events carry the harness owner's *current* projection version rather than
    a frozen constant, so the appearance or retirement of a hold is visible.
    """

    runtime, _, _, events = harness_actor()
    hold(runtime._actor)
    held = events.wait("hold")
    assert held.disposition == "held"
    assert held.version == runtime._actor.version
    release(runtime._actor)
    released = events.wait("release")
    assert released.disposition == "released"
    assert released.version == runtime._actor.version
    assert released.version > held.version
