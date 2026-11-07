import threading
from types import SimpleNamespace

import pytest

from hyprial.kernel.impl.actor_runtime.contracts import AdmissionResult
from hyprial.kernel.impl.actor_runtime.effects import EffectLane, EffectRequest


@pytest.mark.parametrize("kind", ["kernel", "home"])
def test_idle_effect_worker_parks_without_timer_and_close_wakes_it(monkeypatch, kind):
    from hyprial.identity.impl.agents.home import effects as home_effects
    from hyprial.kernel.impl.actor_runtime import effects as kernel_effects

    module = kernel_effects if kind == "kernel" else home_effects
    parked = threading.Event()
    waits = []

    class TrackedCondition(threading.Condition):
        def wait(self, timeout=None):
            waits.append(timeout)
            parked.set()
            return super().wait(timeout)

    # Substitute only this module's threading namespace, not the global
    # threading module or Queue's unrelated synchronization primitives.
    monkeypatch.setattr(module, "threading", SimpleNamespace(
        Condition=TrackedCondition, Thread=threading.Thread, Event=threading.Event,
    ))
    if kind == "kernel":
        lane = module.EffectLane(name="parked", execute=lambda value: value,
                                 complete=lambda event: AdmissionResult.ACCEPTED)
    else:
        lane = module.AgentHomeEffectLane(object(), complete=lambda event: None)
    try:
        assert parked.wait(1), "idle worker must park on its owned condition"
        assert waits and all(timeout is None for timeout in waits)
    finally:
        assert lane.close(1), "close must wake the parked worker"


@pytest.mark.parametrize("kind", ["kernel", "home"])
def test_close_with_unsubmitted_reservation_wakes_after_cancellation(kind):
    from hyprial.identity.impl.agents.home.effects import AgentHomeEffectLane

    if kind == "kernel":
        lane = EffectLane(name="reserved-cancel", execute=lambda value: value,
                          complete=lambda event: AdmissionResult.ACCEPTED)
    else:
        lane = AgentHomeEffectLane(object(), complete=lambda event: None)
    try:
        assert lane.reserve("rollback", 1) is AdmissionResult.ACCEPTED
        assert not lane.close(0.01)
        assert lane.cancel_reservation("rollback", 1)
        assert lane.close(1)
    finally:
        lane.cancel_reservation("rollback", 1)
        assert lane.close(1)


def test_reserved_commit_can_submit_after_close_and_cannot_be_canceled_after_submit():
    lane = None
    offered = threading.Event()

    def complete(event):
        offered.set()
        return AdmissionResult.ACCEPTED

    lane = EffectLane(
        name="reserved", execute=lambda payload: payload, complete=complete,
        capacity=1, workers=3,
    )
    try:
        assert lane.reserve("commit", 1) is AdmissionResult.ACCEPTED
        assert lane.reserve("other", 1) is AdmissionResult.OVERLOADED
        assert not lane.close(0.01)
        assert (
            lane.submit_reserved(EffectRequest("commit", 1, b"committed"))
            is AdmissionResult.ACCEPTED
        )
        assert offered.wait(1)
        assert not lane.cancel_reservation("commit", 1)
        assert lane.acknowledge("commit", 1)
    finally:
        lane.acknowledge("commit", 1)
        assert lane.close(1)


def test_failed_commit_releases_reservation_without_effect():
    calls = []
    lane = EffectLane(
        name="canceled",
        execute=lambda value: calls.append(value),
        complete=lambda event: AdmissionResult.ACCEPTED,
        capacity=1,
    )
    assert lane.reserve("rollback", 1) is AdmissionResult.ACCEPTED
    assert lane.cancel_reservation("rollback", 1)
    assert lane.close(1)
    assert calls == []


def test_capacity_is_retained_until_owner_acknowledges_completion():
    offered = threading.Event()
    events = []
    executions = []

    def complete(event):
        events.append(event)
        offered.set()
        return AdmissionResult.ACCEPTED

    lane = EffectLane(
        name="test-effect",
        execute=lambda value: executions.append(value) or value * 2,
        complete=complete,
        capacity=1,
    )
    try:
        assert lane.submit(EffectRequest("one", 3, 5)) is AdmissionResult.ACCEPTED
        assert offered.wait(1)
        assert events[0].result == 10
        assert lane.submit(EffectRequest("one", 3, 5)) is AdmissionResult.ACCEPTED
        assert lane.submit(EffectRequest("two", 3, 6)) is AdmissionResult.OVERLOADED
        assert not lane.acknowledge("one", 2)
        assert not lane.close(0.01)
        assert lane.snapshot().outstanding == 1
        assert lane.acknowledge("one", 3)
    finally:
        lane.acknowledge("one", 3)
        assert lane.close(1)
    assert executions == [5]


def test_slow_effect_does_not_block_submit_and_close_does_not_cancel_it():
    entered, release, completed = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    lane = None

    def execute(value):
        entered.set()
        assert release.wait(3)
        return value

    def settle(event):
        lane.acknowledge(event.operation_id, event.generation)
        completed.set()
        return AdmissionResult.ACCEPTED

    lane = EffectLane(name="slow-effect", execute=execute, complete=settle, capacity=1)
    try:
        assert (
            lane.submit(EffectRequest("slow", 1, "immutable"))
            is AdmissionResult.ACCEPTED
        )
        assert entered.wait(1)
        assert not lane.close(0.01)
        assert lane.submit(EffectRequest("new", 1, "no")) is AdmissionResult.CLOSED
        release.set()
        assert completed.wait(1)
    finally:
        release.set()
        assert lane.close(1)


def test_completion_rejection_retries_without_repeating_external_effect():
    delivered = threading.Event()
    calls = []
    offers = []
    lane = None

    def complete(event):
        offers.append(event)
        if len(offers) == 1:
            return AdmissionResult.OVERLOADED
        lane.acknowledge(event.operation_id, event.generation)
        delivered.set()
        return AdmissionResult.ACCEPTED

    lane = EffectLane(
        name="retry-effect",
        execute=lambda value: calls.append(value),
        complete=complete,
    )
    try:
        lane.submit(EffectRequest("retry", 7, 42))
        assert delivered.wait(1)
        assert calls == [42]
        assert len(offers) == 2
        assert lane.snapshot().outstanding == 0
    finally:
        assert lane.close(1)


def test_native_base_exception_has_terminal_receipt_and_worker_survives():
    events = []
    delivered = threading.Event()
    lane = None

    def execute(value):
        if value == "fatal":
            raise SystemExit("synthetic native adapter exit")
        return value

    def complete(event):
        events.append(event)
        lane.acknowledge(event.operation_id, event.generation)
        delivered.set()
        return AdmissionResult.ACCEPTED

    lane = EffectLane(name="fatal-custody", execute=execute, complete=complete)
    try:
        lane.submit(EffectRequest("first", 1, "fatal"))
        assert delivered.wait(1)
        assert events[-1].error == "SystemExit"
        delivered.clear()
        lane.submit(EffectRequest("second", 1, "healthy"))
        assert delivered.wait(1)
        assert events[-1].result == "healthy"
        assert lane.snapshot().outstanding == 0
    finally:
        assert lane.close(1)


def test_effect_failure_keeps_class_name_and_bounds_provider_safe_detail():
    events = []
    delivered = threading.Event()
    owner = "owner.person@example.test"

    class DetailedFailure(RuntimeError):
        safe_detail = f"owner {owner} " + "x" * 300

    def execute(value):
        raise DetailedFailure("synthetic-secret")

    def complete(event):
        events.append(event)
        lane.acknowledge(event.operation_id, event.generation)
        delivered.set()
        return AdmissionResult.ACCEPTED

    lane = EffectLane(name="bounded-detail", execute=execute, complete=complete)
    try:
        lane.submit(EffectRequest("detail", 1, None))
        assert delivered.wait(1)
    finally:
        assert lane.close(1)

    assert events[-1].error == "DetailedFailure"
    assert events[-1].detail.startswith("owner [REDACTED_EMAIL] ")
    assert owner not in events[-1].detail
    assert len(events[-1].detail.encode("utf-8")) == 200


def test_effect_failure_without_provider_detail_remains_class_name_only():
    events = []
    delivered = threading.Event()

    def execute(value):
        raise OSError("synthetic-secret should not reach completion detail")

    def complete(event):
        events.append(event)
        lane.acknowledge(event.operation_id, event.generation)
        delivered.set()
        return AdmissionResult.ACCEPTED

    lane = EffectLane(name="class-name-only", execute=execute, complete=complete)
    try:
        lane.submit(EffectRequest("detail", 1, None))
        assert delivered.wait(1)
    finally:
        assert lane.close(1)

    assert events[-1].error == "OSError"
    assert events[-1].detail is None
