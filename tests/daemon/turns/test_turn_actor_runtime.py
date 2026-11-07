from __future__ import annotations

import asyncio
import time
import threading
from collections.abc import Callable
from dataclasses import replace

import pytest

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_ports import (
    CloseTurnPumpCommand,
    EnqueueTurnCommand,
    SteerTurnCommand,
    TurnDeliveryProjection,
    TurnResultProjection,
    TurnSteerIoCompleted,
)
from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_runtime import TurnRuntime
from hyprial.daemon.impl.harnesses.streaming.process import StreamingTurnProcess
from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_runtime import _CompletionRelay, _RecoveryReady


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not satisfied before timeout")


def _enqueue(identifier: str) -> EnqueueTurnCommand:
    return EnqueueTurnCommand(
        correlation_id=f"correlation-{identifier}",
        delivery=TurnDeliveryProjection(
            delivery_id=identifier,
            conversation_id="conversation",
            sender="sender",
            recipient="recipient",
            message=f"prompt-{identifier}",
        ),
    )


def test_steer_folds_a_queued_delivery_into_the_running_turn() -> None:
    started: list[str] = []
    steered: list[tuple[int, int, str, str]] = []
    events: list[object] = []
    runtime = TurnRuntime(
        name="steer-fold",
        capacity=4,
        io_submit=lambda generation, version, delivery: started.append(
            delivery.delivery_id
        ),
        io_interrupt=lambda *args: None,
        io_steer=lambda generation, version, correlation, delivery, message: steered.append(
            (generation, version, delivery, message)
        ),
        io_close=lambda *args: None,
        io_abort=lambda generation, ready: ready(),
        io_failure_decision=lambda *args: None,
        event_sink=events.append,
    )
    try:
        assert runtime.submit(_enqueue("one")) is PortAdmission.ACCEPTED
        _wait_until(lambda: started == ["one"])
        assert runtime.submit(_enqueue("two")) is PortAdmission.ACCEPTED
        assert runtime.submit(
            SteerTurnCommand("steer-two", "two")
        ) is PortAdmission.ACCEPTED
        _wait_until(lambda: len(steered) == 1)
        generation, version, delivery_id, message = steered[0]
        assert (delivery_id, message) == ("two", "prompt-two")
        assert runtime.complete_steer(
            generation=generation,
            version=version,
            correlation_id="steer-two",
            delivery_id="two",
            accepted=True,
            consumed=True,
        ) is PortAdmission.ACCEPTED
        _wait_until(
            lambda: any(
                isinstance(event, TurnSteerIoCompleted) for event in events
            )
        )
        completed = next(
            event
            for event in events
            if isinstance(event, TurnSteerIoCompleted)
        )
        assert completed.accepted is True
        assert completed.consumed is True
        # The folded delivery must not also run as its own turn.
        runtime.complete_io(
            generation=generation,
            version=version,
            result=TurnResultProjection("one", "recipient", "completed", "ok"),
        )
        _wait_until(lambda: len(runtime.read_results()) == 1)
        assert started == ["one"]
    finally:
        runtime.submit(CloseTurnPumpCommand("steer-close", deadline_ms=100))
        assert runtime.drain(1)


def test_advanced_steer_keeps_the_delivery_queued_for_the_next_turn() -> None:
    started: list[str] = []
    steered: list[tuple[int, int, str, str]] = []
    events: list[object] = []
    runtime = TurnRuntime(
        name="steer-advance",
        capacity=4,
        io_submit=lambda generation, version, delivery: started.append(
            delivery.delivery_id
        ),
        io_interrupt=lambda *args: None,
        io_steer=lambda generation, version, correlation, delivery, message: steered.append(
            (generation, version, delivery, message)
        ),
        io_close=lambda *args: None,
        io_abort=lambda generation, ready: ready(),
        io_failure_decision=lambda *args: None,
        event_sink=events.append,
    )
    try:
        assert runtime.submit(_enqueue("one")) is PortAdmission.ACCEPTED
        _wait_until(lambda: started == ["one"])
        assert runtime.submit(_enqueue("two")) is PortAdmission.ACCEPTED
        assert runtime.submit(
            SteerTurnCommand("steer-two", "two")
        ) is PortAdmission.ACCEPTED
        _wait_until(lambda: len(steered) == 1)
        generation, version, _, _ = steered[0]
        assert runtime.complete_steer(
            generation=generation,
            version=version,
            correlation_id="steer-two",
            delivery_id="two",
            accepted=True,
            consumed=False,
        ) is PortAdmission.ACCEPTED
        runtime.complete_io(
            generation=generation,
            version=version,
            result=TurnResultProjection("one", "recipient", "completed", "ok"),
        )
        # ``consumed=False`` means the queued delivery still runs next.
        _wait_until(lambda: started == ["one", "two"])
    finally:
        runtime.submit(CloseTurnPumpCommand("advance-close", deadline_ms=100))
        assert runtime.drain(1)


def test_steer_of_the_running_delivery_is_not_a_queued_steer() -> None:
    steered: list[str] = []
    events: list[object] = []
    runtime = TurnRuntime(
        name="steer-idle",
        capacity=4,
        io_submit=lambda *args: None,
        io_interrupt=lambda *args: None,
        io_steer=lambda *args: steered.append(args[3]),
        io_close=lambda *args: None,
        io_abort=lambda generation, ready: ready(),
        io_failure_decision=lambda *args: None,
        event_sink=events.append,
    )
    try:
        assert runtime.submit(_enqueue("one")) is PortAdmission.ACCEPTED
        assert runtime.submit(
            SteerTurnCommand("steer-one", "one")
        ) is PortAdmission.ACCEPTED
        _wait_until(
            lambda: any(
                getattr(event, "code", None) == "TURN_NOT_QUEUED"
                for event in events
            )
        )
        assert steered == []
    finally:
        runtime.submit(CloseTurnPumpCommand("idle-close", deadline_ms=100))
        assert runtime.drain(1)


def test_steer_with_no_delivery_at_all_is_not_in_flight() -> None:
    steered: list[str] = []
    events: list[object] = []
    runtime = TurnRuntime(
        name="steer-empty",
        capacity=4,
        io_submit=lambda *args: None,
        io_interrupt=lambda *args: None,
        io_steer=lambda *args: steered.append(args[3]),
        io_close=lambda *args: None,
        io_abort=lambda generation, ready: ready(),
        io_failure_decision=lambda *args: None,
        event_sink=events.append,
    )
    try:
        assert runtime.submit(
            SteerTurnCommand("steer-none", "ghost")
        ) is PortAdmission.ACCEPTED
        _wait_until(
            lambda: any(
                getattr(event, "code", None) == "TURN_NOT_IN_FLIGHT"
                for event in events
            )
        )
        assert steered == []
    finally:
        runtime.submit(CloseTurnPumpCommand("empty-close", deadline_ms=100))
        assert runtime.drain(1)


def test_turn_actor_owns_bounded_fifo_and_terminal_projection() -> None:
    started: list[tuple[int, int, str]] = []
    runtime = TurnRuntime(
        name="fifo",
        capacity=2,
        io_submit=lambda generation, version, delivery: started.append(
            (generation, version, delivery.delivery_id)
        ),
        io_interrupt=lambda generation, version, correlation, delivery: None,
        io_close=lambda generation, version, correlation: None,
        io_abort=lambda generation, ready: ready(),
        io_failure_decision=lambda generation, version, delivery, retry, attempt, error: None,
    )

    assert runtime.submit(_enqueue("one")) is PortAdmission.ACCEPTED
    assert runtime.submit(_enqueue("two")) is PortAdmission.ACCEPTED
    assert runtime.submit(_enqueue("three")) is PortAdmission.OVERLOADED
    _wait_until(lambda: len(started) == 1)
    generation, version, _ = started[0]
    assert runtime.complete_io(
        generation=generation,
        version=version,
        result=TurnResultProjection("one", "recipient", "completed", "first"),
    ) is PortAdmission.ACCEPTED
    _wait_until(lambda: len(started) == 2)
    generation, version, _ = started[1]
    runtime.complete_io(
        generation=generation,
        version=version,
        result=TurnResultProjection("two", "recipient", "completed", "second"),
    )
    _wait_until(lambda: len(runtime.read_results()) == 2)

    assert [item.delivery_id for item in runtime.drain_results()] == ["one", "two"]
    assert [item[2] for item in started] == ["one", "two"]
    assert runtime.submit(_enqueue("three")) is PortAdmission.ACCEPTED
    assert runtime.submit(
        CloseTurnPumpCommand("close", deadline_ms=100)
    ) is PortAdmission.ACCEPTED
    assert runtime.drain(1.0) is True


def test_bounded_result_drain_preserves_fifo_and_unclaimed_admission() -> None:
    started = []
    runtime = TurnRuntime(
        name="bounded-result-drain", capacity=2,
        io_submit=lambda generation, version, delivery: started.append(
            (generation, version, delivery.delivery_id)
        ),
        io_interrupt=lambda *args: None,
        io_close=lambda *args: None,
        io_abort=lambda generation, ready: ready(),
        io_failure_decision=lambda *args: None,
    )
    try:
        assert runtime.submit(_enqueue("one")) is PortAdmission.ACCEPTED
        assert runtime.submit(_enqueue("two")) is PortAdmission.ACCEPTED
        for index, identifier in enumerate(("one", "two")):
            _wait_until(lambda: len(started) > index)
            generation, version, _ = started[index]
            runtime.complete_io(
                generation=generation, version=version,
                result=TurnResultProjection(identifier, "recipient", "failed", error="native failure"),
            )
        _wait_until(lambda: len(runtime.read_results()) == 2)
        with pytest.raises(ValueError):
            runtime.drain_results(-1)
        assert runtime.drain_results(0) == ()
        assert runtime.submit(_enqueue("three")) is PortAdmission.OVERLOADED
        assert [result.delivery_id for result in runtime.drain_results(1)] == ["one"]
        assert [result.delivery_id for result in runtime.read_results()] == ["two"]
        assert runtime.submit(_enqueue("three")) is PortAdmission.ACCEPTED
        assert runtime.submit(_enqueue("four")) is PortAdmission.OVERLOADED
        assert [result.delivery_id for result in runtime.drain_results(1)] == ["two"]
        assert runtime.drain_results(1) == ()
    finally:
        runtime.submit(CloseTurnPumpCommand("bounded-close", deadline_ms=100))
        assert runtime.drain(1)


def test_completion_relay_retries_backend_overload_without_losing_final() -> None:
    started: list[tuple[int, int]] = []
    runtime = TurnRuntime(
        name="completion-overload",
        io_submit=lambda generation, version, delivery: started.append(
            (generation, version)
        ),
        io_interrupt=lambda generation, version, correlation, delivery: None,
        io_close=lambda generation, version, correlation: None,
        io_abort=lambda generation, ready: ready(),
        io_failure_decision=lambda generation, version, delivery, retry, attempt, error: None,
    )
    runtime.submit(_enqueue("one"))
    _wait_until(lambda: bool(started))

    original_tell = runtime._runtime.tell  # type: ignore[attr-defined]
    overloads = 3

    def overloaded(handle: object, command: object) -> AdmissionResult:
        nonlocal overloads
        if type(command).__name__ == "_IoCompleted" and overloads:
            overloads -= 1
            return AdmissionResult.OVERLOADED
        return original_tell(handle, command)  # type: ignore[arg-type]

    runtime._runtime.tell = overloaded  # type: ignore[method-assign,attr-defined]
    generation, version = started[0]
    assert runtime.complete_io(
        generation=generation,
        version=version,
        result=TurnResultProjection("one", "recipient", "completed", "final"),
    ) is PortAdmission.ACCEPTED
    _wait_until(lambda: len(runtime.read_results()) == 1)
    assert overloads == 0
    assert runtime.drain_results()[0].output == "final"
    assert runtime.drain(1.0) is True


def test_actor_crash_aborts_generation_and_never_restarts_same_turn() -> None:
    starts: list[str] = []
    accepted: list[tuple[int, int, str]] = []
    aborted: list[int] = []
    first = True

    def submit_io(
        generation: int, version: int, delivery: TurnDeliveryProjection
    ) -> None:
        nonlocal first
        starts.append(delivery.delivery_id)
        if first:
            first = False
            raise RuntimeError("mutant crash after first turn handoff")
        accepted.append((generation, version, delivery.delivery_id))

    def abort(generation: int, ready: Callable[[], None]) -> None:
        aborted.append(generation)
        ready()

    runtime = TurnRuntime(
        name="crash-rebuild",
        capacity=2,
        io_submit=submit_io,
        io_interrupt=lambda generation, version, correlation, delivery: None,
        io_close=lambda generation, version, correlation: None,
        io_abort=abort,
        io_failure_decision=lambda generation, version, delivery, retry, attempt, error: None,
    )
    assert runtime.submit(_enqueue("first")) is PortAdmission.ACCEPTED
    assert runtime.submit(_enqueue("second")) is PortAdmission.ACCEPTED

    _wait_until(lambda: bool(accepted))
    assert starts.count("first") == 1
    assert starts.count("second") == 1
    assert aborted == [1]
    _wait_until(
        lambda: any(item.delivery_id == "first" for item in runtime.read_results())
    )
    failed = next(item for item in runtime.read_results() if item.delivery_id == "first")
    assert failed.status == "failed"

    generation, version, _ = accepted[0]
    runtime.complete_io(
        generation=generation,
        version=version,
        result=TurnResultProjection("second", "recipient", "completed", "ok"),
    )
    _wait_until(lambda: len(runtime.read_results()) == 2)
    runtime.drain_results()
    assert runtime.drain(1.0) is True


def test_actor_owns_retry_budget_and_terminal_abandonment() -> None:
    started: list[tuple[int, int]] = []
    decisions: list[tuple[bool, int]] = []
    runtime = TurnRuntime(
        name="retry-budget",
        max_delivery_attempts=2,
        io_submit=lambda generation, version, delivery: started.append(
            (generation, version)
        ),
        io_interrupt=lambda generation, version, correlation, delivery: None,
        io_close=lambda generation, version, correlation: None,
        io_abort=lambda generation, ready: ready(),
        io_failure_decision=(
            lambda generation, version, delivery, retry, attempt, error: decisions.append(
                (retry, attempt)
            )
        ),
    )
    runtime.submit(_enqueue("one"))
    _wait_until(lambda: bool(started))
    generation, version = started[0]

    runtime.fail_io(
        generation=generation,
        version=version,
        delivery_id="one",
        error="wire disconnected",
    )
    _wait_until(lambda: decisions == [(True, 1)])
    assert runtime.read_results() == ()

    runtime.fail_io(
        generation=generation,
        version=version,
        delivery_id="one",
        error="wire disconnected again",
    )
    _wait_until(lambda: decisions == [(True, 1), (False, 2)])
    [result] = runtime.read_results()
    assert result.status == "failed"
    assert result.error == (
        "delivery abandoned after 2 turn attempts; "
        "last failure: wire disconnected again"
    )
    runtime.drain_results()
    assert runtime.drain(1.0) is True


def test_completion_relay_full_and_closed_paths_have_hard_deadlines() -> None:
    relay = _CompletionRelay(lambda command: AdmissionResult.OVERLOADED)
    command = _RecoveryReady(generation=1, failed_delivery_id=None)
    accepted = 0
    started = time.monotonic()
    while relay.submit(command, timeout=0.01) is PortAdmission.ACCEPTED:
        accepted += 1
    assert accepted >= 8
    assert time.monotonic() - started < 0.5
    assert relay.close(0.05) is False

    closed = _CompletionRelay(lambda command: AdmissionResult.CLOSED)
    assert closed.submit(command, timeout=0.01) is PortAdmission.ACCEPTED
    _wait_until(lambda: closed._queue.unfinished_tasks == 0)  # type: ignore[attr-defined]
    assert closed.close(0.2) is True


def test_query_hang_abort_has_deadline_and_forces_reconnect() -> None:
    query_entered = threading.Event()
    release = threading.Event()
    force_calls: list[bool] = []

    class HangingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt: str) -> None:
            query_entered.set()
            await asyncio.to_thread(release.wait)

        async def receive_response(self):
            if False:
                yield None

        async def interrupt(self) -> None:
            release.set()

    def force_stop() -> None:
        force_calls.append(True)
        release.set()

    process = StreamingTurnProcess(
        harness="test",
        label="hanging query",
        client_factory=HangingClient,
        thread_name="hanging-query-test",
        stop_timeout_seconds=0.05,
        force_stop=force_stop,
        force_stopped=lambda: True,
        force_stop_join_seconds=0.2,
    )
    try:
        assert process.enqueue(
            HarnessDelivery("delivery", "conversation", "sender", "recipient", "prompt")
        )
        assert query_entered.wait(1.0)
        generation = process._active_generation
        assert generation is not None
        ready = threading.Event()
        process._abort_generation_io(generation, ready.set)
        assert ready.wait(0.5)
        assert force_calls == [True]
    finally:
        release.set()
        process.stop()


def test_turn_failure_decision_wait_has_hard_timeout() -> None:
    class IdleClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt: str) -> None:
            return None

        async def receive_response(self):
            if False:
                yield None

        async def interrupt(self) -> None:
            return None

    process = StreamingTurnProcess(
        harness="test",
        label="failure timeout",
        client_factory=IdleClient,
        thread_name="failure-timeout-test",
        stop_timeout_seconds=0.05,
    )
    original = process._turn_runtime.fail_io
    process._turn_runtime.fail_io = (  # type: ignore[method-assign]
        lambda **kwargs: PortAdmission.ACCEPTED
    )
    started = time.monotonic()
    try:
        try:
            process._report_turn_failure(1, 1, "delivery", "failure")
        except RuntimeError as error:
            assert "timed out" in str(error)
        else:
            raise AssertionError("failure decision unexpectedly had no deadline")
        assert time.monotonic() - started < 0.5
    finally:
        process._turn_runtime.fail_io = original  # type: ignore[method-assign]
        process.stop()


def test_new_message_never_steers_a_running_workflow_turn() -> None:
    """A delivery arriving during a workflow turn waits for that turn to end.

    Claude has no non-destructive steer: its ``steer`` interrupts the running
    turn and reports ``advanced``.  Using that path for a workflow-owned turn
    aborts the workflow node (HARNESS_TRANSIENT_FAILURE), so the runtime must
    reject the steer and leave the new delivery queued for the next turn.
    """

    started: list[tuple[int, int, str]] = []
    steered: list[str] = []
    events: list[object] = []
    runtime = TurnRuntime(
        name="workflow-steer",
        capacity=4,
        io_submit=lambda generation, version, delivery: started.append(
            (generation, version, delivery.delivery_id)
        ),
        io_interrupt=lambda *args: None,
        io_steer=lambda generation, version, correlation, delivery, message: steered.append(
            delivery
        ),
        io_close=lambda *args: None,
        io_abort=lambda generation, ready: ready(),
        io_failure_decision=lambda *args: None,
        event_sink=events.append,
    )
    try:
        first = _enqueue("ordinary")
        first = replace(
            first,
            delivery=replace(
                first.delivery,
                batch_cursor="cursor",
                message_ids=("ordinary", "workflow-request:one"),
                workflow=True,
            ),
        )
        assert runtime.submit(first) is PortAdmission.ACCEPTED
        _wait_until(lambda: [item[2] for item in started] == ["ordinary"])
        assert runtime.submit(_enqueue("two")) is PortAdmission.ACCEPTED
        assert runtime.submit(
            SteerTurnCommand("steer-two", "two")
        ) is PortAdmission.ACCEPTED
        _wait_until(
            lambda: any(
                getattr(event, "code", None) == "WORKFLOW_TURN_NOT_STEERABLE"
                for event in events
            )
        )
        assert steered == []
        generation, version, _delivery_id = started[0]
        runtime.complete_io(
            generation=generation,
            version=version,
            result=TurnResultProjection(
                "ordinary", "recipient", "completed", "ok"
            ),
        )
        _wait_until(
            lambda: [item[2] for item in started]
            == ["ordinary", "two"]
        )
        assert steered == []
    finally:
        runtime.submit(CloseTurnPumpCommand("workflow-close", deadline_ms=100))
        assert runtime.drain(1)


def test_unbound_workflow_prefix_is_not_classified_as_workflow() -> None:
    steered: list[str] = []
    runtime = TurnRuntime(
        name="workflow-unbound",
        capacity=4,
        io_submit=lambda *args: None,
        io_interrupt=lambda *args: None,
        io_steer=lambda generation, version, correlation, delivery, message: steered.append(
            delivery
        ),
        io_close=lambda *args: None,
        io_abort=lambda generation, ready: ready(),
        io_failure_decision=lambda *args: None,
        event_sink=lambda _event: None,
    )
    try:
        assert runtime.submit(_enqueue("workflow-ordinary")) is PortAdmission.ACCEPTED
        assert runtime.submit(_enqueue("two")) is PortAdmission.ACCEPTED
        assert runtime.submit(
            SteerTurnCommand("steer-two", "two")
        ) is PortAdmission.ACCEPTED
        _wait_until(lambda: steered == ["two"])
    finally:
        runtime.submit(CloseTurnPumpCommand("unbound-close", deadline_ms=100))
        assert runtime.drain(1)
