from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import pytest
from hyprial.daemon.impl.api import HarnessResultStatus, StreamingHarnessProcess
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses import HarnessLauncher, PtyHarnessProcess
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient, without_pi_reserved_arguments
from hyprial.daemon.impl.harnesses.pi.rpc_process import PiRpcProcess
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel

from ..support import (
    FAKE_PI,
    _prompt,
    delivery,
    fake_process,
    spec,
    wait_for_results,
)


def test_reserved_arguments_are_stripped_and_passthrough_kept() -> None:
    args = (
        "--mode",
        "json",
        "--print",
        "-p",
        "--session-id",
        "caller-must-not-win",
        "--session-id=inline-must-not-win",
        "--continue",
        "--resume",
        "--model",
        "gpt-5.5",
        "--thinking",
        "high",
    )
    assert without_pi_reserved_arguments(args) == (
        "--model",
        "gpt-5.5",
        "--thinking",
        "high",
    )


def test_client_command_owns_rpc_mode_and_session_identity() -> None:
    client = PiRpcClient(
        spec(args=("--mode", "json", "--model", "opus", "--session-id", "loser")),
        "session-ref-1",
        command=("pi",),
    )
    assert client.command == (
        "pi",
        "--model",
        "opus",
        "--mode",
        "rpc",
        "--session-id",
        "session-ref-1",
    )


def test_a_recorded_model_selection_reaches_the_pi_argv_exactly_once() -> None:
    "Desired state's provider/model (not args) must decide what pi runs.\n\n    2026-09-21: a pi was seen running its global default (kimi-coding/k3) while\n    an operator believed a model had been chosen.  That incident was the CLI\n    dropping ``--tier`` (tests/shell/cli/workflows/test_cli_dispatch_start.py); this pins the other\n    half -- a recorded selection survives the desired-state JSON round trip and\n    lands in the argv once, ahead of the connector-owned ``--mode rpc``.\n    "

    recorded = HarnessLaunchSpec(
        harness="pi",
        name="pi-worker",
        headless=True,
        model_provider="deepseek",
        model="deepseek-flash",
    )
    restored = HarnessLaunchSpec.from_json(recorded.to_json(), "provider")
    command = PiRpcClient(restored, "session-ref-1", command=("pi",)).command
    assert command == (
        "pi",
        "--provider",
        "deepseek",
        "--model",
        "deepseek-flash",
        "--mode",
        "rpc",
        "--session-id",
        "session-ref-1",
    )


def test_fake_pi_charset_gate_rejects_colons() -> None:
    """The fake gate is real: a raw colon ref must be refused like pi >=0.76."""

    result = subprocess.run(
        [sys.executable, str(FAKE_PI), "--session-id", "squire:alw:usrb"],
        env={**os.environ, "FAKE_PI_VALIDATE_SESSION_ID": "1"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 42
    assert "outside [A-Za-z0-9._-]" in result.stderr


def test_colon_session_ref_passes_pi_charset_gate_end_to_end() -> None:
    """A squire-style ref reaches pi sanitized and runs a full turn."""

    launch = spec(session_ref="squire:alw:usrb")
    process = PiRpcProcess(
        launch,
        client_factory=lambda: PiRpcClient(
            launch,
            "squire:alw:usrb",
            command=(sys.executable, str(FAKE_PI)),
            env={
                "FAKE_PI_MODE": "settled",
                "FAKE_PI_VALIDATE_SESSION_ID": "1",
            },
            startup_timeout_seconds=10.0,
        ),
    )
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(delivery("delivery-1", "valid charset"))
        [result] = wait_for_results(process, 1)

        assert result.status is HarnessResultStatus.COMPLETED
        assert result.output == _prompt("valid charset done")
    finally:
        process.stop()


def test_process_requires_a_managed_headless_pi_spec() -> None:
    interactive = HarnessLaunchSpec(harness="pi", name="tui", headless=False)
    with pytest.raises(ValueError, match="managed headless"):
        PiRpcProcess(interactive)
    claude = HarnessLaunchSpec(harness="claude", name="cc", headless=True)
    with pytest.raises(ValueError, match="managed headless"):
        PiRpcProcess(claude)


def test_session_ref_is_stable_across_client_reconnects() -> None:
    launch = spec(session_ref="pinned-ref")
    process = PiRpcProcess(
        launch,
        client_factory=lambda: PiRpcClient(
            launch, "pinned-ref", command=(sys.executable, str(FAKE_PI))
        ),
    )
    try:
        assert process.session_ref == "pinned-ref"
        assert isinstance(process, StreamingHarnessProcess)
    finally:
        process.stop()


def test_delayed_continuation_never_becomes_a_premature_empty_result(tmp_path: Path) -> None:
    """F10a: a quiet agent_end is provisional until the native run settles."""
    observed = tmp_path / "intermediate-end"
    release = tmp_path / "continue"
    process = fake_process("delayed-settlement", extra_env={
        "FAKE_PI_END_OBSERVED": str(observed), "FAKE_PI_CONTINUE": str(release),
    })
    try:
        assert process.enqueue(delivery("delivery-long", "long task"))
        deadline = time.monotonic() + 5
        while not observed.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert observed.exists(), "fake native run did not reach its intermediate end"
        time.sleep(1.8)  # Longer than the shipped 1.5-second silence heuristic.
        assert process.drain_results() == (), "unfinished run emitted a terminal result"
        release.touch()
        [result] = wait_for_results(process, 1)
        assert result.status is HarnessResultStatus.COMPLETED
        assert result.output == _prompt("long task done")
    finally:
        release.touch()
        process.stop()


@pytest.mark.parametrize("state", [
    {"isStreaming": True, "isCompacting": False, "pendingMessageCount": 0},
    {"isStreaming": False, "isCompacting": True, "pendingMessageCount": 0},
    {"isStreaming": False, "isCompacting": False, "pendingMessageCount": 1},
    {"isStreaming": False},
    {"isStreaming": False, "isCompacting": False, "pendingMessageCount": False},
])
def test_nonterminal_or_incomplete_native_state_does_not_settle(state: dict, tmp_path: Path) -> None:
    command_log = tmp_path / "commands.jsonl"
    process = fake_process("no-settled", settle_grace_seconds=0.03, command_log=command_log, extra_env={
        "FAKE_PI_NATIVE_STATE": json.dumps(state),
    })
    try:
        assert process.wait_ready(timeout=5)
        assert process.enqueue(delivery("delivery-busy", "wait for native terminal"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if command_log.exists() and "hyprial-settle-" in command_log.read_text():
                break
            time.sleep(0.01)
        assert "hyprial-settle-" in command_log.read_text()
        time.sleep(0.12)
        assert process.drain_results() == ()
        assert not any(event.terminal for event in process.drain_progress())
    finally:
        process.stop()


@pytest.mark.parametrize("continuation", [
    "agent_start", "turn_start", "message_start", "compaction_start", "tool_execution_start",
])
def test_continuation_invalidates_in_flight_idle_probe(
    monkeypatch: pytest.MonkeyPatch, continuation: str,
) -> None:
    """A stale get_state response cannot settle a newer native continuation."""
    async def scenario():
        wire = asyncio.Queue()
        writes = asyncio.Queue()
        client = PiRpcClient(spec(), "probe-race", settle_grace_seconds=0.01)

        async def read(*, timeout=None):
            return await asyncio.wait_for(wire.get(), timeout) if timeout else await wire.get()

        async def write(frame):
            writes.put_nowait(frame)

        monkeypatch.setattr(client, "_read", read)
        monkeypatch.setattr(client, "_write", write)
        await client.query("long task")
        await writes.get()  # prompt

        async def collect():
            return [item async for item in client.receive_response()]

        task = asyncio.create_task(collect())
        try:
            wire.put_nowait({"type": "agent_end", "messages": [], "willRetry": False})
            probe = await asyncio.wait_for(writes.get(), 1)
            assert probe["type"] == "get_state"
            wire.put_nowait({"type": continuation})
            wire.put_nowait({"type": "response", "id": probe["id"], "success": True,
                "data": {"isStreaming": False, "isCompacting": False, "pendingMessageCount": 0}})
            await asyncio.sleep(0.05)
            assert not task.done(), "stale idle state settled the continuation"
            wire.put_nowait({"type": "agent_end", "messages": [
                {"role": "assistant", "content": [{"type": "text", "text": "real final"}]},
            ]})
            wire.put_nowait({"type": "agent_settled"})
            result = await asyncio.wait_for(task, 1)
            assert result[-1].result == "real final"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_missing_agent_settled_requires_positive_native_idle_state(tmp_path: Path) -> None:
    """Older builds complete only after a correlated get_state proves idle."""

    command_log = tmp_path / "commands.jsonl"
    process = fake_process("no-settled", settle_grace_seconds=0.2, command_log=command_log)
    try:
        assert process.enqueue(delivery("delivery-1", "fallback"))
        [result] = wait_for_results(process, 1)

        assert result.status is HarnessResultStatus.COMPLETED
        assert result.output == _prompt("fallback done")
        events = process.drain_progress()
        assert [event.phase for event in events] == [
            "turn-start",
            "retry",
            "turn-end",
        ]
        assert events[-1].summary == "pi turn settled"
        assert events[-1].terminal is True
        commands = [json.loads(line) for line in command_log.read_text().splitlines()]
        assert any(row.get("id", "").startswith("hyprial-settle-") for row in commands)
    finally:
        process.stop()


def test_a_compaction_slower_than_the_startup_budget_still_recovers(
    tmp_path: Path,
) -> None:
    # Compaction summarises ~1M tokens through the model; it is bounded by the
    # turn's own deadline, never by the startup probe's budget.
    command_log = tmp_path / "commands.jsonl"
    process = fake_process(
        "context-exhausted-recovers",
        command_log=command_log,
        startup_timeout_seconds=1.0,
        extra_env={"FAKE_PI_COMPACT_DELAY": "2.5"},
    )
    try:
        assert process.enqueue(delivery("delivery-context", "large turn"))
        [result] = wait_for_results(process, 1)

        assert result.status is HarnessResultStatus.COMPLETED, result.error
        assert result.output == _prompt("large turn done")
    finally:
        process.stop()


def test_runtime_launch_custody_rejects_before_pi_native_spawn() -> None:
    class RejectedCustody:
        def __enter__(self):
            raise RuntimeError("AGENT_RUNTIME_CONTEXT_STALE")

        def __exit__(self, *_args):
            return False

    client = PiRpcClient(
        spec(),
        "stale-session",
        command=("/must-not-spawn/pi",),
        runtime_launch_custody=lambda _context: RejectedCustody(),
    )
    client._runtime_context = object()  # type: ignore[assignment]

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="AGENT_RUNTIME_CONTEXT_STALE"):
            await client.__aenter__()

    asyncio.run(scenario())
    assert not client._spawned.is_set()
    assert client._process is None


def test_missing_final_text_recovers_through_get_last_assistant_text() -> None:
    process = fake_process("no-text")
    try:
        assert process.enqueue(delivery("delivery-1", "quiet turn"))
        [result] = wait_for_results(process, 1)

        assert result.status is HarnessResultStatus.COMPLETED
        assert result.output == "recovered from session"
    finally:
        process.stop()


def test_idle_child_exit_converges_process_health_and_writes_worker_log(
    tmp_path: Path,
) -> None:
    """A settled turn is not process liveness.

    The pi RPC child can exit after the stream ends while the outer pump is
    blocked waiting for its next delivery.  The managed process must surface
    that real child status without needing another inbound message to discover
    EOF, and production launches must leave durable diagnostics behind.
    """

    state_dir = tmp_path / "state"
    channel = build_worker_channel(
        actor="agent:op:node-1:pi-worker",
        session_ref="worker-session",
        hyprial_home=tmp_path / "home",
        state_dir=state_dir,
    )
    launch = spec(session_ref="stable-session")
    process = PiRpcProcess(
        launch,
        client_factory=lambda: PiRpcClient(
            launch,
            "stable-session",
            command=(sys.executable, str(FAKE_PI)),
            env={"FAKE_PI_MODE": "exit-after-turn"},
            worker_channel=channel,
        ),
        worker_channel=channel,
        reconnect_delay_seconds=0.01,
    )
    item = delivery("delivery-1", "finish then exit")
    now_ms = time.time_ns() // 1_000_000
    inbox = InboxService(state_dir / "inbox.sqlite3", MemoryDeliveryTransport())
    assert inbox.receive(
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
    ).acknowledged
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(item)
        [result] = wait_for_results(process, 1)
        assert result.status is HarnessResultStatus.COMPLETED

        deadline = time.monotonic() + 2.0
        while process.running and time.monotonic() < deadline:
            time.sleep(0.01)

        assert process.running is False
        assert "status 23" in (process.last_error or "")
        log_path = state_dir / "logs" / "workers" / "pi-pi-worker.jsonl"
        records = []
        log_deadline = time.monotonic() + 2.0
        while len(records) < 5 and time.monotonic() < log_deadline:
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            if len(records) < 5:
                time.sleep(0.01)
        assert [record["event"] for record in records] == [
            "worker.started",
            "worker.ready",
            "worker.turn.started",
            "worker.turn.completed",
            "worker.exited",
        ]
        for record in records[2:4]:
            assert record["messageId"] == "delivery-1"
            assert record["correlationId"] == "delivery-1"
            assert record["node"] == "worker-turn"
        assert log_path.stat().st_mode & 0o777 == 0o600
        assert all(record["component"] == "pi-worker" for record in records)
        assert all(record["name"] == "pi-worker" for record in records)
        assert all(record["runtime"] == "pi" for record in records)
        assert records[-1]["returnCode"] == 23
        assert "fake pi exited after its first turn" in records[-1]["stderrTail"]
    finally:
        process.stop()
        inbox.close()


def test_overridden_pi_command_keeps_the_pty_seam() -> None:
    launcher = HarnessLauncher(
        commands={"pi": (sys.executable, "-c", "import time; time.sleep(30)")}
    )
    process = launcher.start(spec())
    try:
        assert isinstance(process, PtyHarnessProcess)
    finally:
        process.stop()
