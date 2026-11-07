"""Pump-seam tests for progress events (route C, T2).

The streaming pump collects harness-side ProgressObservation values into a
bounded, drop-oldest queue and stamps them with delivery context; the
terminal turn outcome is never affected by progress traffic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from hyprial.daemon.impl.api import HarnessDelivery, HarnessResultStatus
from hyprial.daemon.impl.harnesses.streaming.protocol import ProgressObservation
from hyprial.daemon.impl.harnesses.streaming.process import StreamingTurnProcess


@dataclass(frozen=True)
class _Outcome:
    result: str
    is_error: bool = False


class _ScriptedClient:
    """Yields a scripted mix of observations and one terminal outcome."""

    running = True
    pid = None

    def __init__(self, script: list[object]) -> None:
        self._script = script

    async def __aenter__(self) -> "_ScriptedClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def query(self, _prompt: str) -> None:
        return None

    async def receive_response(self):  # type: ignore[no-untyped-def]
        for item in self._script:
            yield item

    async def interrupt(self) -> None:
        return None


def _process(script: list[object]) -> StreamingTurnProcess:
    return StreamingTurnProcess(
        harness="pi",
        label="test pump",
        client_factory=lambda: _ScriptedClient(script),
        thread_name="test-progress-pump",
    )


def _delivery(delivery_id: str = "delivery-1") -> HarnessDelivery:
    return HarnessDelivery(
        delivery_id=delivery_id,
        conversation_id="conversation-1",
        sender="agent:op:node-1:requester",
        recipient="agent:op:node-1:worker",
        message="do the work",
    )


def _wait_results(process: StreamingTurnProcess, count: int = 1, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    results = []
    while time.monotonic() < deadline:
        results.extend(process.drain_results())
        if len(results) >= count:
            return results
        time.sleep(0.01)
    raise AssertionError(f"pump produced {len(results)}/{count} results")


def test_observations_are_stamped_with_delivery_context() -> None:
    process = _process(
        [
            ProgressObservation(phase="turn-start", summary="turn begins"),
            ProgressObservation(
                phase="tool-call",
                summary="running read",
                tool_call_id="call-1",
                tool_name="read",
            ),
            ProgressObservation(
                phase="turn-end", summary="turn complete", terminal=True
            ),
            _Outcome("work done"),
        ]
    )
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(_delivery())
        [result] = _wait_results(process)
        assert result.status is HarnessResultStatus.COMPLETED
        assert result.output == "work done"

        events = process.drain_progress()
        assert len(events) == 3
        first, second, third = events
        # The pump owns every delivery-scoped field.
        assert first.delivery_id == "delivery-1"
        assert first.conversation_id == "conversation-1"
        assert first.actor == "agent:op:node-1:worker"
        assert first.harness == "pi"
        assert first.emitted_at_ms > 0
        # seq is per-delivery, from 0, monotonic.
        assert [event.seq for event in events] == [0, 1, 2]
        # The harness's own fields pass through untouched.
        assert second.tool_call_id == "call-1"
        assert second.tool_name == "read"
        assert third.terminal is True
        assert first.dropped_since_seq == 0
        assert process.drain_progress() == ()
    finally:
        process.stop()


def test_seq_resets_for_each_delivery() -> None:
    process = _process(
        [
            ProgressObservation(phase="thinking", summary="thinking"),
            _Outcome("done"),
        ]
    )
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(_delivery("d-1"))
        assert process.enqueue(_delivery("d-2"))
        _wait_results(process, 2)

        events = process.drain_progress()
        assert [event.seq for event in events] == [0, 0]
        assert {event.delivery_id for event in events} == {"d-1", "d-2"}
    finally:
        process.stop()


def test_malformed_observation_is_dropped_without_harming_the_turn() -> None:
    process = _process(
        [
            ProgressObservation(phase="thinking", summary="x" * 201),  # too long
            ProgressObservation(phase="thinking", summary="valid"),
            _Outcome("done"),
        ]
    )
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(_delivery())
        [result] = _wait_results(process)
        assert result.status is HarnessResultStatus.COMPLETED

        events = process.drain_progress()
        assert len(events) == 1
        assert events[0].summary == "valid"
        # Contract-invalid producer output never becomes an event and does
        # not consume a seq -- a producer bug is not a consumer-visible gap.
        assert events[0].seq == 0
    finally:
        process.stop()


def test_progress_queue_is_bounded_and_reports_drops() -> None:
    flood = [
        ProgressObservation(phase="thinking", summary=f"tick {index}")
        for index in range(300)
    ]
    process = _process([*flood, _Outcome("done")])
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(_delivery())
        [result] = _wait_results(process)
        assert result.status is HarnessResultStatus.COMPLETED

        events = process.drain_progress()
        assert len(events) == 256  # the queue bound held
        # Oldest dropped: retained seqs are the newest 44..299 contiguously.
        assert [event.seq for event in events] == list(range(44, 300))
        # Every drop is accounted for exactly once: the whole count rides the
        # first event of this drain batch.
        assert events[0].dropped_since_seq == 44
        assert sum(event.dropped_since_seq for event in events) == 44
    finally:
        process.stop()


def test_turn_without_observations_drains_nothing() -> None:
    process = _process([_Outcome("done")])
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(_delivery())
        [result] = _wait_results(process)
        assert result.status is HarnessResultStatus.COMPLETED
        assert process.drain_progress() == ()
    finally:
        process.stop()


def test_provider_error_result_carries_stable_secret_free_failure_code() -> None:
    process = _process([_Outcome("You've hit your usage limit", is_error=True)])
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(_delivery("usage-limit-1"))
        [result] = _wait_results(process)
        assert result.status is HarnessResultStatus.FAILED
        assert result.error == "You've hit your usage limit"
        assert result.failure_code == "PROVIDER_USAGE_LIMIT"
    finally:
        process.stop()
