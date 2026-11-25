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


def test_completion_waits_in_a_restarting_owner_mailbox_without_duplicates():
    import time

    from hyprial.kernel.impl.actor_runtime import (
        STATE_AUTHORITY,
        ActorSpec,
        ActorState,
        SupervisionPolicy,
    )
    from hyprial.kernel.impl.actor_runtime.effects import EffectCompleted
    from hyprial.kernel.impl.actor_runtime.port import ActorEngineConfig, create_engine

    entered, release = threading.Event(), threading.Event()
    copies: list[tuple[int, str]] = []
    lane = None
    generations = 0

    def factory():
        nonlocal generations
        generations += 1
        generation = generations

        def handle(command):
            if command == "crash":
                entered.set()
                assert release.wait(2.0)
                raise RuntimeError("owner crash")
            if isinstance(command, EffectCompleted):
                copies.append((generation, command.operation_id))
                lane.acknowledge(command.operation_id, command.generation)

        return handle

    engine = create_engine(
        ActorEngineConfig(
            policies={
                STATE_AUTHORITY: SupervisionPolicy(
                    max_restarts=3,
                    restart_window=10.0,
                    base_backoff=0.3,
                    max_backoff=0.3,
                    jitter_ratio=0.0,
                )
            }
        )
    )
    owner = engine.spawn(ActorSpec(name="restarting-owner", handler_factory=factory))
    lane = EffectLane(
        name="restarting-owner-effects",
        execute=lambda value: value,
        complete=lambda event: engine.tell(owner, event),
    )
    try:
        assert engine.tell(owner, "crash") is AdmissionResult.ACCEPTED
        assert entered.wait(1.0)
        release.set()
        deadline = time.monotonic() + 1.0
        while engine.snapshot(owner).state is not ActorState.RESTART_PENDING:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert lane.submit(EffectRequest("op", 1, "payload")) is AdmissionResult.ACCEPTED
        deadline = time.monotonic() + 3.0
        while lane.snapshot().outstanding:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert copies == [(2, "op")]
    finally:
        assert lane.close(1.0)
        assert engine.drain(None, 1.0).complete


def test_owner_gone_dead_letters_once_and_settles_the_token():
    from hyprial.kernel.impl.actor_runtime.effects import OwnerGone

    offers, dead = [], []
    lane = EffectLane(
        name="owner-gone",
        execute=lambda value: value * 2,
        complete=lambda event: offers.append(event) or OwnerGone("stopped"),
        retry_seconds=0.005,
        dead_letter_sink=lambda event, reason: dead.append((event, reason)),
    )
    try:
        assert lane.submit(EffectRequest("op", 1, 21)) is AdmissionResult.ACCEPTED
        deadline = threading.Event()
        for _ in range(200):
            if dead:
                break
            deadline.wait(0.005)
        deadline.wait(0.05)
        assert len(offers) == 1
        assert [(event.operation_id, event.result, reason) for event, reason in dead] == [
            ("op", 42, "stopped")
        ]
        assert lane.snapshot().outstanding == 0
        assert lane.acknowledge("op", 1) is False
    finally:
        assert lane.close(1), "a settled token lets the worker exit"


def test_admission_only_callback_keeps_the_retry_until_acknowledged():
    offers = []
    lane = EffectLane(
        name="closed-owner",
        execute=lambda value: value,
        complete=lambda event: offers.append(event) or AdmissionResult.CLOSED,
        retry_seconds=0.005,
    )
    assert lane.submit(EffectRequest("op", 1, "x")) is AdmissionResult.ACCEPTED
    wait = threading.Event()
    for _ in range(200):
        if len(offers) >= 3:
            break
        wait.wait(0.005)
    assert len(offers) >= 3
    assert lane.snapshot().outstanding == 1
    assert lane.acknowledge("op", 1) is True
    assert lane.close(1)
