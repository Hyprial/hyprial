from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

from hyprial.kernel.impl.actor_runtime import (
    ActorEvent,
    ActorEventKind,
    ActorRuntime,
    ActorSpec,
    ActorState,
    AdmissionResult,
    ExpectedActorError,
    STATE_AUTHORITY,
    SupervisionPolicy,
)
from hyprial.kernel.impl.actor_runtime.guardian import ActorGuardian
from hyprial.kernel.impl.actor_runtime.scheduler import GenerationScheduler


def _wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not satisfied before timeout")


def _test_policy(max_restarts: int = 3) -> dict[str, SupervisionPolicy]:
    return {
        STATE_AUTHORITY: SupervisionPolicy(
            max_restarts=max_restarts,
            restart_window=10.0,
            base_backoff=0.0,
            max_backoff=0.0,
            jitter_ratio=0.0,
        )
    }


def test_bounded_admission_reports_overloaded_then_closed() -> None:
    entered = threading.Event()
    release = threading.Event()
    handled: list[object] = []

    def handle(command: object) -> None:
        handled.append(command)
        if command == "first":
            entered.set()
            assert release.wait(1.0)

    runtime = ActorRuntime(policies=_test_policy())
    actor = runtime.start(
        ActorSpec(name="bounded", handler_factory=lambda: handle, mailbox_capacity=1)
    )

    assert runtime.tell(actor, "first") is AdmissionResult.ACCEPTED
    assert entered.wait(1.0)
    assert runtime.tell(actor, "second") is AdmissionResult.ACCEPTED
    assert runtime.tell(actor, "third") is AdmissionResult.OVERLOADED

    release.set()
    _wait_until(lambda: runtime.snapshot(actor).in_flight == 0)
    report = runtime.drain(1.0)
    assert report.complete is True
    assert handled == ["first", "second"]
    assert runtime.tell(actor, "after-drain") is AdmissionResult.CLOSED


def test_expected_error_is_typed_rejection_and_actor_stays_alive() -> None:
    events: list[ActorEvent] = []
    completed = threading.Event()

    def handle(command: object) -> None:
        if command == "bad-input":
            raise ExpectedActorError("INVALID_INPUT", "input is invalid")
        completed.set()

    runtime = ActorRuntime(event_sink=events.append, policies=_test_policy())
    actor = runtime.start(
        ActorSpec(name="typed-errors", handler_factory=lambda: handle)
    )
    assert runtime.tell(actor, "bad-input") is AdmissionResult.ACCEPTED
    _wait_until(
        lambda: any(event.kind is ActorEventKind.COMMAND_REJECTED for event in events)
    )
    assert runtime.snapshot(actor).state is ActorState.RUNNING
    assert runtime.tell(actor, "valid") is AdmissionResult.ACCEPTED
    assert completed.wait(1.0)
    rejection = next(
        event for event in events if event.kind is ActorEventKind.COMMAND_REJECTED
    )
    assert rejection.code == "INVALID_INPUT"
    assert rejection.detail == "input is invalid"
    assert not any(event.kind is ActorEventKind.CHILD_FAILED for event in events)
    assert runtime.drain(1.0).complete is True


def test_unexpected_failure_restarts_only_failed_child() -> None:
    events: list[ActorEvent] = []
    generations_created = 0
    peer_calls: list[object] = []
    recovered = threading.Event()

    def factory() -> Callable[[object], None]:
        nonlocal generations_created
        generations_created += 1
        this_generation = generations_created

        def handle(command: object) -> None:
            if command == "crash":
                raise RuntimeError(f"boom-{this_generation}")
            recovered.set()

        return handle

    runtime = ActorRuntime(
        event_sink=events.append,
        policies=_test_policy(),
        random_source=lambda: 0.5,
    )
    failed = runtime.start(ActorSpec(name="failed", handler_factory=factory))
    peer = runtime.start(
        ActorSpec(name="peer", handler_factory=lambda: peer_calls.append)
    )

    assert runtime.tell(failed, "crash") is AdmissionResult.ACCEPTED
    _wait_until(
        lambda: (
            runtime.snapshot(failed).state is ActorState.RUNNING
            and runtime.snapshot(failed).generation == 2
        )
    )
    assert runtime.snapshot(peer).generation == 1
    assert runtime.tell(peer, "still-alive") is AdmissionResult.ACCEPTED
    assert runtime.tell(failed, "recovered") is AdmissionResult.ACCEPTED
    assert recovered.wait(1.0)
    _wait_until(lambda: peer_calls == ["still-alive"])
    assert generations_created == 2
    assert any(event.kind is ActorEventKind.CHILD_FAILED for event in events)
    assert any(event.kind is ActorEventKind.CHILD_RESTARTED for event in events)
    assert runtime.drain(1.0).complete is True


def test_restart_returns_every_accepted_never_begun_command_to_owner() -> None:
    entered = threading.Event()
    release = threading.Event()
    undelivered: list[tuple[object, str]] = []

    def factory() -> Callable[[object], None]:
        def handle(command: object) -> None:
            if command == "crash":
                entered.set()
                assert release.wait(1.0)
                raise RuntimeError("boom")

        return handle

    runtime = ActorRuntime(policies=_test_policy())
    actor = runtime.start(
        ActorSpec(
            name="queued-custody",
            handler_factory=factory,
            mailbox_capacity=1,
            undelivered_sink=lambda command, reason: undelivered.append(
                (command, reason)
            ),
        )
    )
    assert runtime.tell(actor, "crash") is AdmissionResult.ACCEPTED
    assert entered.wait(1.0)
    assert runtime.tell(actor, "accepted-behind-crash") is AdmissionResult.ACCEPTED
    assert runtime.tell(actor, "over-capacity") is AdmissionResult.OVERLOADED
    release.set()

    _wait_until(
        lambda: (
            runtime.snapshot(actor).state is ActorState.RUNNING
            and runtime.snapshot(actor).generation == 2
        )
    )
    assert undelivered == [
        ("accepted-behind-crash", "ACTOR_RESTARTED_UNDELIVERED")
    ]
    assert runtime.drain(1.0).complete is True


def test_confirmed_stop_returns_backend_residual_without_timeout_discard() -> None:
    class Endpoint:
        pass

    class Backend:
        def start(self, **_kwargs):  # type: ignore[no-untyped-def]
            return Endpoint()

        def tell(self, _endpoint, _token, _command):  # type: ignore[no-untyped-def]
            return AdmissionResult.ACCEPTED

        def close_admission(self, _endpoint):  # type: ignore[no-untyped-def]
            return None

        def load(self, _endpoint):  # type: ignore[no-untyped-def]
            return (1, 0)

        def stop(self, _endpoint, _timeout):  # type: ignore[no-untyped-def]
            return True

    undelivered = []
    scheduler = GenerationScheduler()
    guardian = ActorGuardian(
        policies=_test_policy(),
        event_sink=lambda _event: None,
        scheduler=scheduler,
        backend=Backend(),  # type: ignore[arg-type]
    )
    actor = guardian.start(
        ActorSpec(
            "confirmed-stop-residual",
            lambda: lambda _command: None,
            undelivered_sink=lambda command, reason: undelivered.append(
                (command, reason)
            ),
        )
    )
    assert guardian.tell(actor, "accepted-never-begun") is AdmissionResult.ACCEPTED
    assert guardian.stop(actor, 0.0)
    assert undelivered == [
        ("accepted-never-begun", "ACTOR_STOPPED_UNDELIVERED")
    ]
    assert scheduler.shutdown(1.0)


def test_restart_budget_exhaustion_quarantines_child() -> None:
    runtime = ActorRuntime(
        policies=_test_policy(max_restarts=2),
        random_source=lambda: 0.5,
    )

    def crash(command: object) -> None:
        raise RuntimeError(f"bad command: {command}")

    actor = runtime.start(ActorSpec(name="crash-loop", handler_factory=lambda: crash))
    for expected_generation in (2, 3):
        assert runtime.tell(actor, "crash") is AdmissionResult.ACCEPTED
        _wait_until(
            lambda: (
                runtime.snapshot(actor).state is ActorState.RUNNING
                and runtime.snapshot(actor).generation == expected_generation
            )
        )
    assert runtime.tell(actor, "crash") is AdmissionResult.ACCEPTED
    _wait_until(lambda: runtime.snapshot(actor).state is ActorState.QUARANTINED)
    snapshot = runtime.snapshot(actor)
    assert snapshot.failures_in_window == 3
    assert runtime.tell(actor, "ignored") is AdmissionResult.CLOSED
    assert runtime.drain(0.1).complete is True


def test_default_sink_structurally_logs_failure_and_quarantine(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="hyprial.actor_runtime")
    runtime = ActorRuntime(
        policies=_test_policy(max_restarts=0),
        random_source=lambda: 0.5,
    )

    def crash(command: object) -> None:
        raise RuntimeError(f"bad command: {command}")

    actor = runtime.start(
        ActorSpec(name="default-observable", handler_factory=lambda: crash)
    )
    assert runtime.tell(actor, "crash") is AdmissionResult.ACCEPTED
    _wait_until(lambda: runtime.snapshot(actor).state is ActorState.QUARANTINED)

    events = [
        record.actor_event
        for record in caplog.records
        if record.name == "hyprial.actor_runtime" and hasattr(record, "actor_event")
    ]
    assert [event["kind"] for event in events] == [
        "child_failed",
        "child_quarantined",
    ]
    assert all(event["actorId"] == actor.actor_id for event in events)
    assert all("detail" not in event for event in events)
    assert runtime.drain(0.1).complete is True


def test_restart_factory_failures_consume_budget_and_quarantine() -> None:
    factory_calls = 0
    events: list[ActorEvent] = []

    def factory() -> Callable[[object], None]:
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls > 1:
            raise RuntimeError(f"factory-failed-{factory_calls}")

        def crash(command: object) -> None:
            raise RuntimeError(f"initial-crash-{command}")

        return crash

    runtime = ActorRuntime(
        event_sink=events.append,
        policies=_test_policy(max_restarts=2),
        random_source=lambda: 0.5,
    )
    actor = runtime.start(ActorSpec(name="factory-crash", handler_factory=factory))
    assert runtime.tell(actor, "go") is AdmissionResult.ACCEPTED
    _wait_until(lambda: runtime.snapshot(actor).state is ActorState.QUARANTINED)

    assert factory_calls == 3
    assert runtime.snapshot(actor).failures_in_window == 3
    assert [
        event.code for event in events if event.kind is ActorEventKind.CHILD_FAILED
    ] == ["RuntimeError", "RuntimeError", "RuntimeError"]
    assert runtime.drain(0.1).complete is True


def test_drain_has_a_hard_deadline_and_can_be_retried() -> None:
    entered = threading.Event()
    release = threading.Event()

    def block(command: object) -> None:
        del command
        entered.set()
        assert release.wait(1.0)

    runtime = ActorRuntime(policies=_test_policy())
    actor = runtime.start(ActorSpec(name="blocked", handler_factory=lambda: block))
    assert runtime.tell(actor, "work") is AdmissionResult.ACCEPTED
    assert entered.wait(1.0)
    started = time.monotonic()
    report = runtime.drain(0.03)
    elapsed = time.monotonic() - started
    assert report.complete is False
    assert report.remaining == (actor,)
    assert elapsed < 0.2
    assert runtime.tell(actor, "new-work") is AdmissionResult.CLOSED

    release.set()
    _wait_until(lambda: runtime.snapshot(actor).in_flight == 0)
    assert runtime.drain(1.0).complete is True


@pytest.mark.perf_bound
def test_stopping_200_agent_actors_returns_threads_to_baseline() -> None:
    baseline = set(threading.enumerate())
    runtime = ActorRuntime(policies=_test_policy())
    actors = [
        runtime.start(
            ActorSpec(
                name=f"lifecycle-probe-{index}",
                handler_factory=lambda: lambda _command: None,
            )
        )
        for index in range(200)
    ]
    try:
        assert all(runtime.stop(actor, timeout=2.0) for actor in actors)
        _wait_until(
            lambda: len(set(threading.enumerate()) - baseline) <= 2,
            timeout=3.0,
        )
        assert len(set(threading.enumerate()) - baseline) <= 2
    finally:
        runtime.drain(3.0)


def test_timed_out_explicit_stop_keeps_endpoint_for_later_cleanup() -> None:
    entered = threading.Event()
    release = threading.Event()
    undelivered: list[tuple[object, str]] = []

    def block(command: object) -> None:
        del command
        entered.set()
        assert release.wait(1.0)

    runtime = ActorRuntime(policies=_test_policy())
    actor = runtime.start(
        ActorSpec(
            name="stop-timeout",
            handler_factory=lambda: block,
            undelivered_sink=lambda command, reason: undelivered.append(
                (command, reason)
            ),
        )
    )
    assert runtime.tell(actor, "work") is AdmissionResult.ACCEPTED
    assert entered.wait(1.0)
    assert runtime.stop(actor, timeout=0.02) is False
    assert runtime.tell(actor, "new-work") is AdmissionResult.CLOSED

    release.set()
    _wait_until(lambda: runtime.snapshot(actor).in_flight == 0)
    assert runtime.drain(1.0).complete is True
    assert undelivered == []
