"""Provider-level errors must surface verbatim and stop retry amplification.

Card 260 / spec 2026-09-04: two machines, two providers, one shape.  pi's
``agent_end`` carried an assistant message with ``stopReason:"error"`` and the
provider's own ``errorMessage`` ("fetch failed" on openai-codex; an OAuth
refresh failure on kimi), while hyprial reported only "pi returned no final
assistant text" and retried by attempt count alone.  The reason was in hand
on the same message object and was thrown away; the retries could not heal an
expired token, so they multiplied cost without any chance of success
(f2lt: three turns, two of them inside seven seconds).

The stub shapes below are copied verbatim from the two incident session
files (``~/.pi/agent/sessions/...f2lt.../2026-09-04T01-57-56...jsonl`` and
the crh session behind reviewer message 68630233) -- empty ``content``,
all-zero ``usage``, ``stopReason:"error"`` -- because the bug was precisely
that fields riding on that message were never read.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.api import HarnessDelivery, HarnessResultStatus
from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient
from hyprial.daemon.impl.harnesses.pi.rpc_process import PiRpcProcess
from hyprial.daemon.impl.harnesses.streaming import StreamingTurnProcess
from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_ports import (
    EnqueueTurnCommand,
    TurnDeliveryProjection,
)
from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_runtime import TurnRuntime
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)

from tests.daemon.harnesses.pi.rpc.process.support import FAKE_PI, delivery, spec

FETCH_FAILED = "fetch failed"
KIMI_OAUTH = (
    "OAuth refresh failed for kimi-coding: Kimi Code token refresh "
    "unauthorized (status 400): The provided authorization grant is invalid"
)


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never held")


def _provider_error_process(mode: str, state_dir: Path | None = None) -> PiRpcProcess:
    launch = spec(session_ref="stable-session")
    channel = None
    if state_dir is not None:
        channel = build_worker_channel(
            actor="agent:op:node-1:pi-worker",
            session_ref="worker-session",
            hyprial_home=state_dir / "home",
            state_dir=state_dir / "state",
        )
    return PiRpcProcess(
        launch,
        client_factory=lambda: PiRpcClient(
            launch,
            "stable-session",
            command=(sys.executable, str(FAKE_PI)),
            env={"FAKE_PI_MODE": mode},
            worker_channel=channel,
            startup_timeout_seconds=10.0,
        ),
        worker_channel=channel,
        reconnect_delay_seconds=0.01,
    )


def _single_result(process: PiRpcProcess) -> Any:
    item = delivery("delivery-1", "do the work")
    _ensure_pending(process, item)
    assert process.enqueue(item)
    deadline = time.monotonic() + 5.0
    results = []
    while time.monotonic() < deadline:
        results.extend(process.drain_results())
        if results:
            return results[0]
        time.sleep(0.01)
    raise AssertionError("pi RPC pump produced no result")


def _ensure_pending(process: PiRpcProcess, item: HarnessDelivery) -> None:
    channel = process.worker_channel
    if channel is None:
        return
    now_ms = time.time_ns() // 1_000_000
    inbox = InboxService(
        channel.state_dir / "inbox.sqlite3", MemoryDeliveryTransport()
    )
    try:
        received = inbox.receive(
            InboxMessage(
                message_id=item.delivery_id,
                conversation_id=item.conversation_id,
                sender="agent:op:sender-node:requester",
                recipient=item.recipient,
                payload=item.message.encode(),
                intent="request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=now_ms,
            ),
            now_ms=now_ms,
        )
        assert received.acknowledged
    finally:
        inbox.close()


def _worker_log_records(state_dir: Path, event: str) -> list[dict[str, Any]]:
    # build_worker_channel received state_dir=tmp/"state", so the shared
    # worker route lands at tmp/state/logs/workers/pi-<name>.jsonl.
    log_path = state_dir / "state" / "logs" / "workers" / "pi-pi-worker.jsonl"
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("event") == event
    ]


# --- box 1: the verbatim provider text is the failure reason ----------------


def test_provider_stop_error_is_reported_verbatim() -> None:
    """stopReason=error ⇒ errorMessage verbatim, never the generic text.

    The crh incident shape: fetch failed.  Before the fix the same message
    object carried the reason and the connector still said "pi returned no
    final assistant text".
    """

    process = _provider_error_process("provider-error")
    try:
        result = _single_result(process)
        assert result.status is HarnessResultStatus.FAILED
        assert result.error == FETCH_FAILED
    finally:
        process.stop()


def test_provider_auth_error_is_reported_verbatim() -> None:
    """The f2lt/kimi shape: an OAuth refresh failure must reach hyprial verbatim."""

    process = _provider_error_process("provider-auth-error")
    try:
        result = _single_result(process)
        assert result.status is HarnessResultStatus.FAILED
        assert result.error == KIMI_OAUTH
    finally:
        process.stop()


def test_provider_auth_error_classifies_permanent() -> None:
    """The daemon stops redelivering on a permanent code, so it must classify.

    With the verbatim text in result.error the existing classifier maps
    "unauthorized" to PROVIDER_AUTHENTICATION_FAILED; under the generic text
    every auth failure stayed HARNESS_TRANSIENT_FAILURE and redelivered.
    """

    process = _provider_error_process("provider-auth-error")
    try:
        result = _single_result(process)
        assert result.failure_code == "PROVIDER_AUTHENTICATION_FAILED"
    finally:
        process.stop()


def test_earlier_text_does_not_mask_a_terminal_provider_error() -> None:
    """A run whose last assistant message errored did not answer.

    agent_settled means pi will not continue; an earlier partial text is not
    the final answer, and reporting it as COMPLETED would hide the failure.
    """

    from hyprial.daemon.impl.harnesses.pi.messages import _assistant_provider_error

    messages = [
        {"role": "assistant", "content": [{"type": "text", "text": "partial"}]},
        {
            "role": "assistant",
            "content": [],
            "stopReason": "error",
            "errorMessage": FETCH_FAILED,
        },
    ]
    assert _assistant_provider_error(messages) == FETCH_FAILED


# --- box 3: worker.turn.failed carries reason + providerError ---------------


def test_worker_turn_failed_carries_reason_and_provider_error(tmp_path: Path) -> None:
    """The reader of the worker log must not open pi's session file.

    pi-f2lt.jsonl held three worker.turn.failed lines whose every informative
    field was the generic text; the OAuth reason existed only in the pi
    session file.  The event must name the shape (reason) and carry the
    provider's own words (providerError).
    """

    process = _provider_error_process("provider-auth-error", state_dir=tmp_path)
    try:
        result = _single_result(process)
        assert result.status is HarnessResultStatus.FAILED

        records: list[dict[str, Any]] = []
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            records = _worker_log_records(tmp_path, "worker.turn.failed")
            if records:
                break
            time.sleep(0.01)
        assert records, "no worker.turn.failed record was written"
        [record] = records
        # "token refresh" is diagnostic wording, not a credential value, so
        # the redactor now preserves it verbatim (review round 2 S5) -- the
        # provider's own words survive into the worker log unchanged.
        expected_logged = KIMI_OAUTH
        assert record["providerError"] == expected_logged
        assert record["reason"] == "provider stopReason=error"
        assert record["failure"] == expected_logged
    finally:
        process.stop()


# --- box 1 negative: the truly empty reply keeps the old path ---------------


def test_empty_reply_reports_the_last_assistant_shape(tmp_path: Path) -> None:
    """No error stop, no text, fallback empty ⇒ a diagnostic outcome.

    An empty reply is a different failure (the incident behind the
    "response ended without a terminal result" wording) and keeps the
    harness's existing attempt budget, but the worker log must carry the
    stop reason, content block types, and usage already present on the event.
    """

    process = _provider_error_process("empty", state_dir=tmp_path)
    try:
        result = _single_result(process)
        assert result.status is HarnessResultStatus.FAILED
        assert result.error is not None
        assert result.error.startswith("pi returned no final assistant text")
        assert "stopReason=stop" in result.error
        assert "contentTypes=[thinking]" in result.error
        assert "usage(input=12" in result.error
        assert "output=0" in result.error
        assert "cacheRead=34" in result.error
        assert "cacheWrite=5" in result.error
        [record] = _worker_log_records(tmp_path, "worker.turn.failed")
        assert record["failure"] == result.error
    finally:
        process.stop()


# --- box 2: the retry decision knows the error category ---------------------


def _runtime_with_recorder(
    max_attempts: int,
) -> tuple[TurnRuntime, list[tuple[bool, int]], list[tuple[int, int]]]:
    started: list[tuple[int, int]] = []
    decisions: list[tuple[bool, int]] = []
    runtime = TurnRuntime(
        name="provider-category",
        max_delivery_attempts=max_attempts,
        io_submit=lambda generation, version, delivery: started.append(
            (generation, version)
        ),
        io_interrupt=lambda generation, version, correlation, delivery: None,
        io_close=lambda generation, version, correlation: None,
        io_abort=lambda generation, ready: ready(),
        io_failure_decision=(
            lambda generation, version, delivery, retry, attempt, error: (
                decisions.append((retry, attempt))
            )
        ),
    )
    return runtime, decisions, started


@pytest.mark.parametrize(
    "error_text",
    [
        "429 too many requests",
        "HTTP 429 rate_limit: slow down",
        "quota exceeded for this billing period",
    ],
)
def test_rate_limit_text_is_terminal_on_first_attempt(error_text: str) -> None:
    """§8.2: an exhausted rate limit cannot heal inside one turn.

    Retrying it only multiplies real model-vendor calls; the durable inbox
    owns retry with backoff, and pi's own retry layers are capped at zero
    for a managed launch.
    """

    runtime, decisions, started = _runtime_with_recorder(3)
    try:
        runtime.submit(_enqueue("one"))
        _wait_until(lambda: bool(started))
        generation, version = started[0]
        runtime.fail_io(
            generation=generation,
            version=version,
            delivery_id="one",
            error=error_text,
        )
        _wait_until(lambda: bool(decisions))
        assert decisions == [(False, 1)]
    finally:
        runtime.drain(1.0)


def test_kimi_oauth_text_is_terminal_on_first_attempt() -> None:
    """Auth/expired-class text: retrying an invalid grant can never succeed."""

    runtime, decisions, started = _runtime_with_recorder(3)
    try:
        runtime.submit(_enqueue("one"))
        _wait_until(lambda: bool(started))
        generation, version = started[0]
        runtime.fail_io(
            generation=generation,
            version=version,
            delivery_id="one",
            error=KIMI_OAUTH,
        )
        _wait_until(lambda: bool(decisions))
        assert decisions == [(False, 1)]
    finally:
        runtime.drain(1.0)


@pytest.mark.parametrize(
    "error_text",
    [
        "wire disconnected",
        "request timeout",
        "service unavailable",
        "fetch failed",
    ],
)
def test_non_auth_failures_keep_the_attempt_budget(error_text: str) -> None:
    """Everything outside the auth and usage-limit lists retries as before.

    The parametrized strings are the ones a too-broad table would most
    easily swallow (review round 2 B2): timeouts, vendor 5xx, and network
    fetch wording are transient classes -- pi's own retryable table carries
    "fetch failed" verbatim -- so the budget must run in full for them:
    [(True,1),(True,2),(False,3)].  A 429 is deliberately NOT here: §8.2
    moved rate limits to the terminal class (see the test below).
    """

    runtime, decisions, started = _runtime_with_recorder(3)
    try:
        runtime.submit(_enqueue("one"))
        _wait_until(lambda: bool(started))
        generation, version = started[0]
        for expected_attempt in (1, 2, 3):
            runtime.fail_io(
                generation=generation,
                version=version,
                delivery_id="one",
                error=error_text,
            )
            _wait_until(lambda: len(decisions) >= expected_attempt)
        assert decisions == [(True, 1), (True, 2), (False, 3)]
    finally:
        runtime.drain(1.0)


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


# --- ruling follow-up: both _log_turn sites, with and without fields ------


class _Recorder:
    """Capture structured log calls as the daemon's logger receives them."""

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


class _GenericErrorOutcomeClient:
    """A harness whose error result has no provider_error provenance.

    This is the claude/codex shape through the same StreamingTurnProcess
    pump: the outcome object simply lacks the attribute.  The ruling: the
    shared _log_turn call must not break or write placeholder fields for
    harnesses that never produce a provider error.
    """

    async def __aenter__(self) -> "_GenericErrorOutcomeClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def query(self, prompt: str) -> None:
        del prompt

    async def interrupt(self) -> None:
        return None

    async def receive_response(self) -> Any:
        class _Outcome:
            result = "403 permission_error: weekly quota exhausted"
            is_error = True
            # no provider_error attribute at all

        yield _Outcome()


class _DyingClient:
    """A harness transport that dies mid-turn (the exception-path site).

    """

    failure_text: str = "wire disconnected"

    async def __aenter__(self) -> "_DyingClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def query(self, prompt: str) -> None:
        del prompt

    async def interrupt(self) -> None:
        return None

    async def receive_response(self) -> Any:
        raise ConnectionError(type(self).failure_text)
        yield  # pragma: no cover - marks this as an async generator


def _streaming_process(client_factory: Any, recorder: _Recorder) -> StreamingTurnProcess:
    return StreamingTurnProcess(
        harness="pi",
        label="test pi",
        client_factory=client_factory,
        thread_name="test-provider-log-fields",
        logger=recorder,  # type: ignore[arg-type]
        reconnect_delay_seconds=0.01,
        reconnect_delay_max_seconds=0.05,
        max_delivery_attempts=3,
    )


def test_result_failure_without_provider_error_omits_the_fields() -> None:
    """Ruling condition 1, result-path site: omit, never a None placeholder."""

    recorder = _Recorder()
    process = _streaming_process(_GenericErrorOutcomeClient, recorder)
    try:
        assert process.enqueue(delivery("delivery-generic", "busy work"))
        _wait_until(lambda: bool(recorder.failures()))
        [record] = recorder.failures()
        assert "weekly quota exhausted" in str(record.get("failure", ""))
        assert "providerError" not in record
        assert "reason" not in record
    finally:
        process.stop()


def test_exception_failure_with_provider_marker_logs_provider_error() -> None:
    """Ruling condition 2, exception-path site: the marker table applies.

    The crh shape reaches this site as a transport exception whose text
    carries the provider's words (pi exiting on fetch failures).  The event
    must quote it and, because the same table decides retryability, the
    attempt must already be terminal at 1.
    """

    class _AuthExpired(_DyingClient):
        # Auth-class wording survives in the exception text (a provider
        # process dying on an expired credential), so the marker table
        # still applies on this path; network wording no longer does
        # (review round 2 B1).
        failure_text = (
            "pi RPC session exited unexpectedly: "
            "unauthorized (401): OAuth token refresh failed"
        )

    recorder = _Recorder()
    process = _streaming_process(_AuthExpired, recorder)
    try:
        assert process.enqueue(delivery("delivery-crash", "busy work"))
        _wait_until(lambda: bool(recorder.failures()))
        [record] = recorder.failures()
        assert record["providerError"] == _AuthExpired.failure_text
        assert record["reason"] == "provider error"
        assert record["attempt"] == 1
        assert _AuthExpired.failure_text in str(record.get("failure", ""))
    finally:
        process.stop()


def test_exception_failure_without_marker_omits_the_fields() -> None:
    """Ruling condition 2, exception-path site: plain failures unchanged."""

    recorder = _Recorder()
    process = _streaming_process(_DyingClient, recorder)
    try:
        assert process.enqueue(delivery("delivery-plain", "busy work"))
        # No marker ⇒ the attempt budget runs in full: three failures, not
        # one -- the negative mirror of the terminal-at-1 case above.
        _wait_until(lambda: len(recorder.failures()) >= 3)
        records = recorder.failures()
        assert [record["attempt"] for record in records] == [1, 2, 3]
        for record in records:
            assert record["failure"] == "wire disconnected"
            assert "providerError" not in record
            assert "reason" not in record
    finally:
        process.stop()
