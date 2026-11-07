"""#277 ruling: no timeout ever kills a turn.

The wall-clock cap is retired (managed and interactive alike); the
producer-local quiet watch reports ``worker.turn.stalled`` /
``worker.turn.resumed`` instead of interrupting; the generic pieces from
issue #270 (real-cause failure labels, replay backoff, abandoned terminal
state) survive untouched.  Every test here is a mutation gate from the
acceptance list: restoring a deadline, interrupting on expiry, reporting
with threshold 0, or crashing on a legacy ``turnTimeoutSeconds`` spec must
turn at least one of these red.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.api import HarnessDelivery, HarnessResultStatus
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.codex.carrier import (
    MANAGED_TURN_IDLE_TIMEOUT_SECONDS,
    )
from hyprial.daemon.impl.harnesses.codex.client import (
    CodexAppServerClient,
    CodexAppServerProcess,
)
from hyprial.daemon.impl.harnesses.dsh.client import DshApiClient
from hyprial.daemon.impl.harnesses.streaming.protocol import (
    TURN_IDLE_TIMEOUT_ENV,
    TURN_TIMEOUT_ENV,
    ProgressObservation,
    resolve_turn_timeout_seconds,
)
from hyprial.daemon.impl.harnesses.streaming.process import (
    StreamingTurnProcess,
    )
from hyprial.kernel.impl.log import Logger

FAKE_CODEX = Path(__file__).resolve().parents[3] / 'tests/fixtures/harnesses/fake_codex_app_server.py'


@pytest.fixture(autouse=True)
def _clear_timeout_env(monkeypatch) -> None:
    monkeypatch.delenv(TURN_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(TURN_IDLE_TIMEOUT_ENV, raising=False)


def codex_spec(
    turn_timeout_seconds: float | None = None,
    idle_timeout_seconds: float | None = None,
) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness="codex",
        name="codex-worker",
        headless=True,
        cwd=str(Path.cwd()),
        turn_timeout_seconds=turn_timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
    )


def delivery(delivery_id: str = "delivery-1") -> HarnessDelivery:
    return HarnessDelivery(
        delivery_id=delivery_id,
        conversation_id="conversation-1",
        sender="requester",
        recipient="agent:op:node-1:worker",
        message="do the work",
    )


def run_to_terminal_result(
    spec: HarnessLaunchSpec,
    mode: str,
    wire_log: Path,
    *,
    timeout: float = 20.0,
):
    """Drive one delivery through a fake app-server to its terminal result.

    Returns (result, progress_events): the terminal HarnessResult plus every
    progress event drained along the way.
    """

    process = CodexAppServerProcess(
        spec,
        command=(sys.executable, str(FAKE_CODEX)),
        env={"FAKE_CODEX_MODE": mode, "FAKE_CODEX_LOG": str(wire_log)},
        reconnect_delay_seconds=0.01,
        max_delivery_attempts=1,
    )
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(delivery())
        deadline = time.monotonic() + timeout
        results: list[Any] = []
        events: list[Any] = []
        while time.monotonic() < deadline and not results:
            events.extend(process.drain_progress())
            results.extend(process.drain_results())
            time.sleep(0.05)
        events.extend(process.drain_progress())
        assert results, f"{mode} delivery never reached a terminal result"
        [result] = results
        return result, events
    finally:
        process.stop()


def wire_methods(wire_log: Path) -> list[str]:
    if not wire_log.exists():
        return []
    return [
        json.loads(line).get("method")
        for line in wire_log.read_text(encoding="utf-8").splitlines()
    ]


# --- acceptance 1: nothing kills a long turn -------------------------------


def test_turn_far_past_any_former_cap_completes_and_delivers(
    tmp_path: Path,
) -> None:
    """A 2s turn with an (ignored) 0.5s legacy cap in the spec completes
    normally.  Restoring the wall-clock deadline turns this red."""

    result, _ = run_to_terminal_result(
        codex_spec(turn_timeout_seconds=0.5),
        "slow-complete",
        tmp_path / "wire.jsonl",
    )
    assert result.status is HarnessResultStatus.COMPLETED
    assert result.output == (
        "[Harness Network message from requester to agent:op:node-1:worker]\n"
        "do the work done"
    )


def test_dsh_turn_outlives_the_retired_cap() -> None:
    """DSH: the configured turn timeout is parsed but never enforced."""

    class Api:
        def __init__(self) -> None:
            self.polls = 0

        async def call(self, method: str, payload: dict[str, object]) -> object:
            if method == "host.describe":
                return {}
            if method == "session.create":
                return {"sessionId": "session-1"}
            if method == "session.prompt":
                return {"accepted": True}
            if method == "session.history":
                self.polls += 1
                if self.polls < 12:
                    return {"events": []}
                return {
                    "events": [
                        {
                            "event": {
                                "type": "turn/end",
                                "seq": 5,
                                "data": {"reason": {"kind": "completed"}},
                            }
                        }
                    ]
                }
            raise AssertionError(f"unexpected DSH method {method}")

    async def scenario() -> None:
        client = DshApiClient(
            HarnessLaunchSpec(harness="dsh", name="dsh-worker", headless=True),
            api=Api(),
            poll_interval_seconds=0.02,
            # Far below the elapsed polling time; pre-#277 this killed the
            # turn with a cap outcome.
            turn_timeout_seconds=0.05,
        )
        async with client:
            await client.query("long quiet work")
            outcomes = [outcome async for outcome in client.receive_response()]
        [outcome] = outcomes
        assert outcome.is_error is False

    asyncio.run(scenario())


# --- acceptance 2: stalled is reported, never killed -----------------------


def test_quiet_turn_reports_stalled_then_resumed_and_still_completes(
    tmp_path: Path,
) -> None:
    """2.5s of silence against a 0.8s sensitivity: turn-stalled goes up the
    progress channel, activity clears it with turn-resumed, the turn then
    completes -- and NO turn/interrupt was ever sent (the "never killed"
    assertion; interrupting on expiry turns this red)."""

    wire_log = tmp_path / "wire.jsonl"
    result, events = run_to_terminal_result(
        codex_spec(idle_timeout_seconds=0.8), "quiet-then-complete", wire_log
    )
    assert result.status is HarnessResultStatus.COMPLETED
    phases = [event.phase for event in events]
    assert "turn-stalled" in phases
    assert "turn-resumed" in phases
    assert phases.index("turn-stalled") < phases.index("turn-resumed")
    stalled = next(event for event in events if event.phase == "turn-stalled")
    assert stalled.detail is not None
    assert stalled.detail["thresholdSeconds"] == 0.8
    assert stalled.detail["quietSeconds"] >= 0.8
    assert "turn/interrupt" not in wire_methods(wire_log)


def test_persistent_silence_refires_stalled_each_threshold(
    tmp_path: Path,
) -> None:
    """The report re-fires per threshold of continued silence (a slow
    heartbeat), so a long-stalled turn cannot scroll out of view."""

    process = CodexAppServerProcess(
        codex_spec(idle_timeout_seconds=0.5),
        command=(sys.executable, str(FAKE_CODEX)),
        env={"FAKE_CODEX_MODE": "hold-inprogress"},
        reconnect_delay_seconds=0.01,
        max_delivery_attempts=1,
    )
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(delivery())
        deadline = time.monotonic() + 10.0
        stalled_events: list[Any] = []
        while time.monotonic() < deadline and len(stalled_events) < 2:
            stalled_events.extend(
                event
                for event in process.drain_progress()
                if event.phase == "turn-stalled"
            )
            time.sleep(0.05)
        assert len(stalled_events) >= 2, "stalled report did not re-fire"
        assert all(
            event.detail and event.detail["thresholdSeconds"] == 0.5
            for event in stalled_events
        )
    finally:
        process.stop()


def test_pump_persists_stall_conditions_to_the_worker_log(
    tmp_path: Path,
) -> None:
    """turn-stalled / turn-resumed observations land in the worker JSONL as
    worker.turn.stalled / worker.turn.resumed with delivery context."""

    class Scripted:
        running = True
        pid = None

        async def __aenter__(self) -> "Scripted":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def query(self, _prompt: str) -> None:
            return None

        async def receive_response(self):  # type: ignore[no-untyped-def]
            yield ProgressObservation(
                phase="turn-stalled",
                summary="quiet",
                detail={"quietSeconds": 1.5, "thresholdSeconds": 1.0},
            )
            yield ProgressObservation(
                phase="turn-resumed",
                summary="active again",
                detail={"quietSeconds": 1.6, "thresholdSeconds": 1.0},
            )
            yield type("Outcome", (), {"result": "done", "is_error": False})()

        async def interrupt(self) -> None:
            return None

    logger = Logger.worker(tmp_path / "state", runtime="codex", name="worker")
    process = StreamingTurnProcess(
        harness="codex",
        label="stall pump",
        client_factory=Scripted,
        thread_name="test-stall-pump",
        logger=logger,
    )
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(delivery("delivery-stall"))
        deadline = time.monotonic() + 5.0
        results: list[Any] = []
        while time.monotonic() < deadline and not results:
            results.extend(process.drain_results())
            time.sleep(0.02)
        [result] = results
        assert result.status is HarnessResultStatus.COMPLETED
        records = [
            json.loads(line)
            for line in logger.path.read_text(encoding="utf-8").splitlines()
        ]
        [stalled] = [r for r in records if r["event"] == "worker.turn.stalled"]
        [resumed] = [r for r in records if r["event"] == "worker.turn.resumed"]
        assert stalled["messageId"] == "delivery-stall"
        assert stalled["quietSeconds"] == 1.5
        assert stalled["thresholdSeconds"] == 1.0
        assert resumed["messageId"] == "delivery-stall"
    finally:
        process.stop()


# --- acceptance 3: threshold 0 disables reporting --------------------------


def test_zero_sensitivity_never_reports_stalled(tmp_path: Path) -> None:
    result, events = run_to_terminal_result(
        codex_spec(idle_timeout_seconds=0),
        "quiet-then-complete",
        tmp_path / "wire.jsonl",
    )
    assert result.status is HarnessResultStatus.COMPLETED
    assert all(event.phase != "turn-stalled" for event in events)


# --- acceptance 4: legacy configuration keeps loading ----------------------


def test_legacy_turn_timeout_spec_loads_and_is_ignored() -> None:
    spec = HarnessLaunchSpec.from_json(
        {
            "provider": "codex",
            "name": "worker",
            "headless": True,
            "turnTimeoutSeconds": 900,
            "idleTimeoutSeconds": 300,
        },
        "spec",
    )
    assert spec.turn_timeout_seconds == 900.0
    # Round-trip preserved for older daemons across the version boundary.
    assert spec.to_json()["turnTimeoutSeconds"] == 900.0
    client = CodexAppServerClient(spec)
    # The retired cap leaves no enforcement state behind ...
    assert not hasattr(client, "_turn_timeout_seconds")
    # ... while the quiet-watch sensitivity still resolves from the spec.
    assert client._turn_idle_timeout_seconds == 300.0


def test_sensitivity_resolution_and_zero_disable(monkeypatch) -> None:
    assert (
        CodexAppServerClient(codex_spec())._turn_idle_timeout_seconds
        == MANAGED_TURN_IDLE_TIMEOUT_SECONDS
        == 900.0
    )
    monkeypatch.setenv(TURN_IDLE_TIMEOUT_ENV, "120")
    assert (
        CodexAppServerClient(codex_spec())._turn_idle_timeout_seconds == 120.0
    )
    assert (
        CodexAppServerClient(
            codex_spec(idle_timeout_seconds=5.0)
        )._turn_idle_timeout_seconds
        == 5.0
    )
    monkeypatch.setenv(TURN_IDLE_TIMEOUT_ENV, "0")
    assert CodexAppServerClient(
        codex_spec()
    )._turn_idle_timeout_seconds == math.inf
    monkeypatch.setenv(TURN_IDLE_TIMEOUT_ENV, "soon")
    with pytest.raises(ValueError, match="must be a number"):
        resolve_turn_timeout_seconds(
            None, default=1.0, env_var=TURN_IDLE_TIMEOUT_ENV
        )


# --- kept generic pieces: replay backoff + abandoned terminal state --------


class _AlwaysFailingClient:
    running = True
    pid = None

    def __init__(self, query_times: list[float]) -> None:
        self._query_times = query_times

    async def __aenter__(self) -> "_AlwaysFailingClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def query(self, _prompt: str) -> None:
        self._query_times.append(time.monotonic())
        raise ConnectionError("Codex app-server input closed (simulated)")

    def receive_response(self):  # pragma: no cover - query always raises
        raise AssertionError("unreachable")

    async def interrupt(self) -> None:
        return None


def test_replay_backs_off_exponentially_and_abandons_with_a_record(
    tmp_path: Path,
) -> None:
    query_times: list[float] = []
    logger = Logger.worker(tmp_path / "state", runtime="codex", name="worker")
    process = StreamingTurnProcess(
        harness="codex",
        label="backoff pump",
        client_factory=lambda: _AlwaysFailingClient(query_times),
        thread_name="test-backoff-pump",
        logger=logger,
        reconnect_delay_seconds=0.1,
        reconnect_delay_max_seconds=5.0,
        max_delivery_attempts=4,
    )
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(delivery("delivery-backoff"))
        deadline = time.monotonic() + 10.0
        results: list[Any] = []
        while time.monotonic() < deadline and not results:
            results.extend(process.drain_results())
            time.sleep(0.02)
        assert results, "failing delivery never became terminal"
        [result] = results

        assert result.status is HarnessResultStatus.FAILED
        assert result.error is not None
        assert "abandoned after 4 turn attempts" in result.error
        assert "input closed" in result.error

        assert len(query_times) == 4
        gaps = [
            later - earlier
            for earlier, later in zip(query_times, query_times[1:])
        ]
        for gap, minimum in zip(gaps, (0.1, 0.2, 0.4)):
            assert gap >= minimum - 0.01, f"gaps {gaps} lost the backoff"

        records = [
            json.loads(line)
            for line in logger.path.read_text(encoding="utf-8").splitlines()
        ]
        failures = [
            record for record in records if record["event"] == "worker.turn.failed"
        ]
        assert [record["attempt"] for record in failures] == [1, 2, 3, 4]
        # The real mechanism, not the class name (issue #81 fix point).
        assert all("input closed" in record["failure"] for record in failures)
        [abandoned] = [
            record
            for record in records
            if record["event"] == "worker.turn.abandoned"
        ]
        assert abandoned["attempts"] == 4
    finally:
        process.stop()
