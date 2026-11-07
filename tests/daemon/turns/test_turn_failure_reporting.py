"""A failed turn must log why it failed.

Three pi workers retried one delivery roughly 280,000 times across 45 hours.
Every line of that log read ``failure="failed"`` -- the event was already
named ``worker.turn.failed``, so the field repeated the event name while the
actual reason sat unused in the same result object.  The cause had to be
reconstructed afterwards from the harness's own session file.

The reason is not decoration: the two failure exits mean different things.
An error result is the model reporting a problem; an empty result is a turn
that produced nothing, which is what a context grown past the model's limit
looks like.  A log rendering both as "failed" cannot tell them apart, and
telling them apart was the whole diagnosis.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import threading
import time
from typing import Any, Iterator

import pytest

from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.harnesses.streaming.process import StreamingTurnProcess


class _Recorder:
    """Captures structured log calls as the daemon's logger receives them."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def info(self, event: str, **fields: Any) -> None:
        with self._lock:
            self.events.append((event, fields))

    def __getattr__(self, _name: str) -> Any:
        return lambda *_a, **_k: None

    def failures(self) -> list[dict[str, Any]]:
        with self._lock:
            return [f for event, f in self.events if event == "worker.turn.failed"]


def _delivery(delivery_id: str) -> HarnessDelivery:
    return HarnessDelivery(
        delivery_id=delivery_id,
        conversation_id="conversation-1",
        sender="requester",
        recipient="agent:op:node-1:pi-worker",
        message="hello",
    )


def _wait_for(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never held")


def _drain_failure(process: StreamingTurnProcess, delivery_id: str) -> None:
    # The counter is updated when the completion relay accepts the result,
    # before the actor necessarily publishes it. Keep collecting as the daemon
    # does; one empty drain cannot release the reserved delivery ID.
    results = []

    def collected() -> bool:
        results.extend(process.drain_results())
        return bool(results)

    _wait_for(collected)
    assert len(results) == 1
    assert results[0].delivery_id == delivery_id
    assert results[0].status.value == "failed"


@contextmanager
def _completion_after_first_drain(
    process: StreamingTurnProcess,
    monkeypatch: pytest.MonkeyPatch,
    delayed: bool,
) -> Iterator[None]:
    """Force the legal ordering: counter update, empty drain, result publish."""
    release = threading.Event()
    if delayed:
        relay = process._turn_runtime._relay
        deliver = relay._deliver
        drain = process.drain_results

        def deliver_after_drain(completion: Any) -> Any:
            release.wait()
            return deliver(completion)

        def drain_then_release() -> Any:
            results = drain()
            if not release.is_set():
                assert not results
                release.set()
            return results

        monkeypatch.setattr(relay, "_deliver", deliver_after_drain)
        monkeypatch.setattr(process, "drain_results", drain_then_release)
    try:
        yield
    finally:
        # Release the relay before process.stop(), even on assertion failure.
        release.set()


class _EmptyResultClient:
    """A harness answering with an empty string -- the incident's shape.

    stopReason was ``stop`` and the content was one empty text block, so
    nothing raised and ``_receive_result`` took its ``if not output`` exit.
    """

    async def __aenter__(self) -> "_EmptyResultClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def query(self, prompt: str) -> None:
        del prompt

    async def receive_response(self) -> Any:
        class _Outcome:
            result = ""
            is_error = False

        yield _Outcome()

    async def interrupt(self) -> None:
        return None


class _ErrorResultClient(_EmptyResultClient):
    """The other failure exit: the harness reports an error result."""

    async def receive_response(self) -> Any:
        class _Outcome:
            result = "403 permission_error: weekly quota exhausted"
            is_error = True

        yield _Outcome()


class _InterruptedResultClient(_EmptyResultClient):
    """A provider that reports its turn as interrupted after daemon stop."""

    interrupted = threading.Event()

    async def receive_response(self) -> Any:
        await asyncio.to_thread(type(self).interrupted.wait)

        class _Outcome:
            result = "Codex turn turn-1 ended with status 'interrupted'"
            is_error = True

        yield _Outcome()

    async def interrupt(self) -> None:
        type(self).interrupted.set()


def _run_until_failure(client: type, delivery_id: str) -> dict[str, Any]:
    recorder = _Recorder()
    process = StreamingTurnProcess(
        harness="pi",
        label="test pi",
        client_factory=client,
        thread_name=f"test-{delivery_id}",
        logger=recorder,  # type: ignore[arg-type]
        reconnect_delay_seconds=0.01,
        max_delivery_attempts=2,
    )
    try:
        assert process.enqueue(_delivery(delivery_id))
        _wait_for(lambda: recorder.failures())
        return recorder.failures()[0]
    finally:
        process.stop()


def test_empty_result_failure_logs_its_reason_not_the_status() -> None:
    """The literal reason must reach the log.

    Pinned against the reason's own text, not ``result.status.value``:
    asserting the status equals the status passes whatever the code does,
    which is exactly how this field became uninformative.

    To make it red, restore ``{"failure": result.status.value}`` -- the
    substring below vanishes and only the word "failed" remains.
    """

    failure = _run_until_failure(_EmptyResultClient, "delivery-empty")
    assert "response ended without a terminal result" in str(failure.get("failure", ""))


def test_error_result_failure_carries_the_harness_text() -> None:
    """The other exit must be distinguishable from the first.

    Both were logged as "failed" before; if only one exit carried its reason
    the log still could not separate "the model objected" from "the model
    returned nothing".
    """

    failure = _run_until_failure(_ErrorResultClient, "delivery-error")
    assert "weekly quota exhausted" in str(failure.get("failure", ""))


@pytest.mark.parametrize("delayed_completion", [False, True])
def test_one_delivery_failing_repeatedly_is_counted(
    monkeypatch: pytest.MonkeyPatch, delayed_completion: bool
) -> None:
    """A delivery that keeps failing must be visible as such.

    The incident's whole tell was one delivery failing over and over, and no
    surface carried it: three workers did that for 45 hours unnoticed. Counted
    per delivery rather than as a rate, because worker turns run from seconds
    to tens of minutes -- a "failures per second" threshold either fires on a
    slow healthy worker or misses a fast-looping one.

    To make this red, drop the _record_turn_outcome call: the count stays 0
    while the same delivery fails indefinitely, which is the state the
    incident ran in.
    """

    recorder = _Recorder()
    process = StreamingTurnProcess(
        harness="pi",
        label="test pi",
        client_factory=_EmptyResultClient,
        thread_name="test-repeat-count",
        logger=recorder,  # type: ignore[arg-type]
        reconnect_delay_seconds=0.001,
        reconnect_delay_max_seconds=0.01,
    )
    try:
        with _completion_after_first_drain(process, monkeypatch, delayed_completion):
            # One at a time, as the inbox redelivers it.  draining stands in for
            # the daemon: a delivery id stays reserved until its result is
            # drained, so the redelivery the incident ran on only becomes
            # admissible once the daemon has collected the failure -- which is
            # exactly what the daemon does before redelivering.
            for expected in (1, 2, 3):
                _wait_for(lambda: process.enqueue(_delivery("delivery-repeating")))
                _wait_for(
                    lambda expected=expected: (
                        process.repeated_failure_count >= expected
                    ),
                    timeout=10.0,
                )
                _drain_failure(process, "delivery-repeating")
            assert process.repeated_failure_delivery_id == "delivery-repeating"
            assert process.repeated_failure_count >= 3
    finally:
        process.stop()


@pytest.mark.parametrize("delayed_completion", [False, True])
def test_a_successful_turn_clears_the_repeat_count(
    monkeypatch: pytest.MonkeyPatch, delayed_completion: bool
) -> None:
    """Otherwise the signal latches and stops meaning "currently stuck"."""

    recorder = _Recorder()

    class _FailsThenSucceeds(_EmptyResultClient):
        attempts = 0

        async def receive_response(self) -> Any:
            type(self).attempts += 1
            failed = type(self).attempts == 1

            class _Outcome:
                result = "" if failed else "done"
                is_error = False

            yield _Outcome()

    process = StreamingTurnProcess(
        harness="pi",
        label="test pi",
        client_factory=_FailsThenSucceeds,
        thread_name="test-repeat-clear",
        logger=recorder,  # type: ignore[arg-type]
        reconnect_delay_seconds=0.001,
        reconnect_delay_max_seconds=0.01,
    )
    try:
        with _completion_after_first_drain(process, monkeypatch, delayed_completion):
            _wait_for(lambda: process.enqueue(_delivery("delivery-recovers")))
            _wait_for(lambda: process.repeated_failure_count >= 1, timeout=10.0)
            _drain_failure(process, "delivery-recovers")
            _wait_for(lambda: process.enqueue(_delivery("delivery-recovers")))
            _wait_for(lambda: process.repeated_failure_count == 0, timeout=10.0)
    finally:
        process.stop()


def test_failure_code_is_reported_alongside_the_reason() -> None:
    """The daemon classifies permanence from this code, so it must be visible.

    Reading it out of the log is what lets an operator tell "the daemon will
    retry this" from "the daemon has given up" without reading daemon state.
    """

    failure = _run_until_failure(_EmptyResultClient, "delivery-code")
    assert failure.get("failureCode") == "HARNESS_TRANSIENT_FAILURE"


def test_daemon_initiated_graph_settle_logs_interrupted_not_failed() -> None:
    """A graph-settle stop is terminal observability, not harness failure.

    The delivery-keyed marker stands in for the daemon's durable PAC cleanup
    decision.  Without consuming that marker, today's provider outcome is
    logged as ``worker.turn.failed`` with ``HARNESS_TRANSIENT_FAILURE``.
    """

    _InterruptedResultClient.interrupted.clear()
    recorder = _Recorder()
    delivery = _delivery("delivery-graph-settled")
    process = StreamingTurnProcess(
        harness="codex",
        label="test codex",
        client_factory=_InterruptedResultClient,
        thread_name="test-graph-settled-interrupt",
        logger=recorder,  # type: ignore[arg-type]
        reconnect_delay_seconds=0.01,
    )
    try:
        assert process.enqueue(delivery)
        _wait_for(
            lambda: any(event == "worker.turn.started" for event, _ in recorder.events)
        )
        # This is daemon-owned state, set before the stop reaches the provider.
        process.prepare_daemon_interruption("graph-settled")
        process.stop()

        interrupted = [
            fields
            for event, fields in recorder.events
            if event == "worker.turn.interrupted"
        ]
        failed = [
            fields
            for event, fields in recorder.events
            if event == "worker.turn.failed"
        ]
        assert not failed
        assert len(interrupted) == 1
        assert interrupted[0]["reason"] == "graph-settled"
    finally:
        process.stop()


def test_provider_initiated_interruption_stays_failed() -> None:
    """Provider-side interruption text is not a daemon interruption record."""

    _InterruptedResultClient.interrupted.clear()
    recorder = _Recorder()
    delivery = _delivery("delivery-provider-interrupted")
    process = StreamingTurnProcess(
        harness="codex",
        label="test codex",
        client_factory=_InterruptedResultClient,
        thread_name="test-provider-interrupted",
        logger=recorder,  # type: ignore[arg-type]
        reconnect_delay_seconds=0.01,
        max_delivery_attempts=1,
    )
    try:
        assert process.enqueue(delivery)
        _wait_for(
            lambda: any(event == "worker.turn.started" for event, _ in recorder.events)
        )
        _InterruptedResultClient.interrupted.set()
        _wait_for(lambda: recorder.failures())

        failure = recorder.failures()[0]
        assert failure["failureCode"] == "HARNESS_TRANSIENT_FAILURE"
        assert not any(
            event == "worker.turn.interrupted" for event, _ in recorder.events
        )
    finally:
        process.stop()
