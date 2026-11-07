from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.api import HarnessResultStatus
from hyprial.daemon.impl.harnesses import HarnessLauncher, HarnessStartError

from ..support import (
    _prompt,
    delivery,
    fake_process,
    spec,
    wait_for_results,
)


def test_turn_completes_on_agent_settled_with_final_assistant_text() -> None:
    process = fake_process("settled")
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.dispatch_ready is True
        assert process.enqueue(delivery("delivery-1", "do the work"))
        [result] = wait_for_results(process, 1)

        assert result.delivery_id == "delivery-1"
        assert result.recipient == "agent:op:node-1:pi-worker"
        assert result.status is HarnessResultStatus.COMPLETED
        assert result.output == _prompt("do the work done")
    finally:
        process.stop()


def test_pi_coarse_events_become_progress_observations() -> None:
    """Route C T5: lifecycle/tool boundaries surface; delta streams do not."""

    process = fake_process("progress")
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(delivery("delivery-progress", "show progress"))
        [result] = wait_for_results(process, 1)
        assert result.status is HarnessResultStatus.COMPLETED

        events = process.drain_progress()
        assert [event.phase for event in events] == [
            "turn-start",  # agent_start
            "turn-start",  # turn_start
            "message-segment",
            "tool-call",
            "tool-result",
            "compaction",
            "compaction",
            "retry",
            "retry",
            "message-segment",
            "turn-end",
            "turn-end",  # agent_settled, terminal
        ]
        assert [event.seq for event in events] == list(range(len(events)))
        tool_call = events[3]
        assert tool_call.tool_call_id == "call-1"
        assert tool_call.tool_name == "read"
        assert events[-1].terminal is True
        # tool_execution_update was on the wire but deliberately produced no
        # event; route C never consumes the partial-result stream.
        assert not any("partial" in event.summary for event in events)
    finally:
        process.stop()


def test_fifo_order_is_preserved_across_turns() -> None:
    process = fake_process("settled")
    try:
        for index in range(1, 4):
            assert process.enqueue(delivery(f"delivery-{index}", f"task {index}"))
        results = wait_for_results(process, 3)

        assert [item.delivery_id for item in results] == [
            "delivery-1",
            "delivery-2",
            "delivery-3",
        ]
        assert [item.output for item in results] == [
            _prompt("task 1 done"),
            _prompt("task 2 done"),
            _prompt("task 3 done"),
        ]
    finally:
        process.stop()


def test_rejected_prompt_fails_the_delivery_for_daemon_retry() -> None:
    process = fake_process("reject")
    try:
        assert process.enqueue(delivery("delivery-1", "busy work"))
        [result] = wait_for_results(process, 1)

        assert result.status is HarnessResultStatus.FAILED
        assert "busy" in (result.error or "")
    finally:
        process.stop()


def test_context_exhaustion_compacts_once_then_retries_the_turn(
    tmp_path: Path,
) -> None:
    command_log = tmp_path / "commands.jsonl"
    process = fake_process("context-exhausted-recovers", command_log=command_log)
    try:
        assert process.enqueue(delivery("delivery-context", "large turn"))
        [result] = wait_for_results(process, 1)

        assert result.status is HarnessResultStatus.COMPLETED
        assert result.output == _prompt("large turn done")
        commands = [
            json.loads(line) for line in command_log.read_text(encoding="utf-8").splitlines()
        ]
        assert [item["type"] for item in commands].count("compact") == 1
        assert [item["type"] for item in commands].count("prompt") == 2
    finally:
        process.stop()


def test_interrupt_aborts_the_turn_and_marks_it_non_terminal() -> None:
    process = fake_process("abort")
    try:
        assert process.enqueue(delivery("delivery-1", "long task"))
        deadline = time.monotonic() + 5.0
        while not process.interrupt("delivery-1", timeout=1.0):
            assert time.monotonic() < deadline, "turn never became active"
            time.sleep(0.01)
        [result] = wait_for_results(process, 1)

        assert result.status is HarnessResultStatus.INTERRUPTED
        # A non-terminal outcome releases the dedupe key for redelivery.
        assert process.enqueue(delivery("delivery-1", "long task"))
        # The redelivered turn must be interrupted too: in "abort" mode a
        # turn only ends when interrupted, and stop() can miss the interrupt
        # race (active delivery not yet published), which would leave the
        # pump thread -- and its event loop and child pipes -- wedged past
        # the join timeout (card #77; stop-race itself reported separately).
        deadline = time.monotonic() + 5.0
        while not process.interrupt("delivery-1", timeout=1.0):
            assert time.monotonic() < deadline, "redelivered turn never became active"
            time.sleep(0.01)
        wait_for_results(process, 1)
    finally:
        process.stop()


def test_mid_turn_crash_reconnects_and_retries_the_head_delivery(
    tmp_path: Path,
) -> None:
    process = fake_process("crash", state_file=tmp_path / "crash-marker")
    try:
        assert process.enqueue(delivery("delivery-1", "survive this"))
        [result] = wait_for_results(process, 1, timeout=10.0)

        assert result.status is HarnessResultStatus.COMPLETED
        assert result.output == _prompt("survive this done")
        assert (tmp_path / "crash-marker").exists()
    finally:
        process.stop()


def test_default_launcher_gives_headless_pi_a_streaming_process() -> None:
    launched = []

    class FakeRpcProcess:
        running = True

        def wait_ready(self, *, timeout: float) -> bool:
            assert timeout > 0
            return True

        def stop(self) -> None:
            self.running = False

    process = FakeRpcProcess()
    launcher = HarnessLauncher(
        pi_rpc_factory=lambda launch: launched.append(launch) or process
    )
    launch = spec(args=("--model", "opus"))

    assert launcher.start(launch) is process
    assert launched == [launch]


def test_default_launcher_refuses_headless_pi_that_never_became_ready() -> None:
    """Defect C: a pi worker that never answered its ready probe is not started.

    2026-10-16 production: ``hyprial start`` returned ``ok:true`` while no pi
    child existed, because the launcher's ``rpc`` branch returned the process
    before readiness (unlike codex/dsh/python-worker).  The failure lived only
    in ``last_error``.  This pins the readiness gate.
    """

    class NotReadyProcess:
        running = True
        p2_managed = True
        last_error = "Pi P2 headless launch requires explicit --approve trust"

        def __init__(self) -> None:
            self.stopped = False

        def wait_ready(self, *, timeout: float) -> bool:
            assert timeout > 0
            return False

        def stop(self) -> None:
            self.stopped = True

    process = NotReadyProcess()
    launcher = HarnessLauncher(pi_rpc_factory=lambda _launch: process)

    with pytest.raises(HarnessStartError) as raised:
        launcher.start(spec(args=("--approve",)))

    assert process.stopped is True
    assert "--approve" in str(raised.value)


def test_default_launcher_refuses_a_p2_cwd_outside_its_own_workspace(
    tmp_path: Path,
) -> None:
    """The P2 trust refusal is synchronous: the start never spawns a child."""

    from types import SimpleNamespace

    from hyprial.daemon.impl.harnesses.pi.loader import PiProjectTrustError
    from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
    from hyprial.kernel import HarnessLaunchSpec

    workspace = tmp_path / "agents" / "pi-worker" / "workspace"
    workspace.mkdir(parents=True)
    workspace.chmod(0o700)
    channel = build_worker_channel(
        actor="agent:op:node-1:pi-worker",
        session_ref="session-1",
        hyprial_home=tmp_path / "home",
        state_dir=tmp_path / "state",
        runtime_context=SimpleNamespace(
            roots=SimpleNamespace(workspace_root=workspace)
        ),
    )
    assert channel.runtime_context is not None
    launcher = HarnessLauncher(worker_channel_factory=lambda _spec: channel)
    launch = HarnessLaunchSpec(
        harness="pi", name="pi-worker", headless=True, cwd=str(tmp_path)
    )

    with pytest.raises(PiProjectTrustError):
        launcher.start(launch)
