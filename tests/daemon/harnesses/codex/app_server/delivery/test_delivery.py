from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
import pytest
from hyprial.daemon.impl.api import HarnessResultStatus, StreamingHarnessProcess
from hyprial.daemon.impl.harnesses.codex import _server_request_response, CodexAppServerClient
from hyprial.daemon.impl.harnesses.streaming.turn_io import (
    _SteerRejected,
    _SteerUnknown,
)

from ..support import (
    FAKE_CODEX,
    _prompt,
    delivery,
    fake_process,
    read_log,
    spec,
    wait_for_results,
)


def test_initialize_start_turn_completion_and_authoritative_read(tmp_path: Path) -> None:
    log_path = tmp_path / "wire.jsonl"
    process = fake_process("complete", log_path=log_path)
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.session_ref == "codex-thread-1"
        assert isinstance(process, StreamingHarnessProcess)
        assert process.enqueue(delivery("delivery-1", "do the work"))
        [result] = wait_for_results(process, 1)

        assert result.status is HarnessResultStatus.COMPLETED
        assert result.output == _prompt("do the work done")
        methods = [message.get("method") for message in read_log(log_path)]
        assert methods[:3] == ["initialize", "initialized", "thread/start"]
        assert methods[-2:] == ["turn/start", "thread/read"]
    finally:
        process.stop()


def test_codex_lifecycle_notifications_become_progress_observations() -> None:
    """Route C T6: exact 0.147 lifecycle shapes; delta notifications excluded."""

    process = fake_process("progress")
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(delivery("delivery-progress", "show progress"))
        [result] = wait_for_results(process, 1)
        assert result.status is HarnessResultStatus.COMPLETED

        events = process.drain_progress()
        assert [event.phase for event in events] == [
            "turn-start",
            "tool-call",
            "message-segment",
            "tool-result",
            "turn-end",
        ]
        assert [event.seq for event in events] == list(range(len(events)))
        command_call = events[1]
        assert command_call.tool_call_id == "command-1"
        assert command_call.tool_name == "commandExecution"
        assert command_call.summary == "calling commandExecution"
        plan = events[2]
        assert plan.detail == {
            "totalSteps": 2,
            "completedSteps": 1,
            "currentStep": "report",
        }
        command_result = events[3]
        assert command_result.detail is not None
        assert command_result.detail["exitCode"] == 0
        assert events[-1].terminal is True
        # The fake emitted item/agentMessage/delta between item start and
        # completion; no observation carries that partial text.
        assert not any("partial" in event.summary for event in events)
    finally:
        process.stop()


def test_fifo_order_is_preserved_across_app_server_turns() -> None:
    process = fake_process("complete")
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


def test_interrupt_uses_turn_interrupt_and_marks_delivery_non_terminal(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "wire.jsonl"
    process = fake_process("interrupt", log_path=log_path)
    try:
        assert process.enqueue(delivery("delivery-1", "long task"))
        deadline = time.monotonic() + 5.0
        while not process.interrupt("delivery-1", timeout=1.0):
            assert time.monotonic() < deadline, "turn never became active"
            time.sleep(0.01)
        [result] = wait_for_results(process, 1)

        assert result.status is HarnessResultStatus.INTERRUPTED
        assert process.enqueue(delivery("delivery-1", "long task"))
        assert "turn/interrupt" in [
            message.get("method") for message in read_log(log_path)
        ]
    finally:
        process.stop()


def test_interrupt_is_rejected_until_codex_accepts_turn_start(
    tmp_path: Path,
) -> None:
    """An in-flight delivery is not yet an interruptible native Codex turn."""

    log_path = tmp_path / "wire.jsonl"
    turn_start_entered = tmp_path / "turn-start-entered"
    release_turn_start = tmp_path / "release-turn-start"
    process = fake_process(
        "delay-turn-start",
        log_path=log_path,
        state_path=turn_start_entered,
        release_path=release_turn_start,
    )
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(delivery("delivery-race", "long task"))
        deadline = time.monotonic() + 5.0
        while not turn_start_entered.exists():
            assert time.monotonic() < deadline, "turn/start never reached server"
            time.sleep(0.001)

        # The pump has selected this delivery, but Codex has not returned a
        # native turn ID.  Reporting success here loses the caller's retry.
        assert not process.interrupt("delivery-race", timeout=1.0)
        assert "turn/interrupt" not in {
            message.get("method") for message in read_log(log_path)
        }

        release_turn_start.touch()
        deadline = time.monotonic() + 5.0
        while not process.interrupt("delivery-race", timeout=1.0):
            assert (
                time.monotonic() < deadline
            ), "accepted turn never became interruptible"
            time.sleep(0.001)

        [result] = wait_for_results(process, 1)
        assert result.status is HarnessResultStatus.INTERRUPTED
        assert [
            message.get("method") for message in read_log(log_path)
        ].count("turn/interrupt") == 1
    finally:
        release_turn_start.touch()
        process.stop()


def test_turn_steer_uses_active_turn_precondition(tmp_path: Path) -> None:
    log_path = tmp_path / "wire.jsonl"

    async def drive() -> None:
        client = CodexAppServerClient(
            spec(),
            command=(sys.executable, str(FAKE_CODEX)),
            env={"FAKE_CODEX_MODE": "hold", "FAKE_CODEX_LOG": str(log_path)},
        )
        async with client:
            await client.query("first")
            turn_id = client.active_turn_id
            assert turn_id is not None
            assert await client.steer_turn("follow up") == turn_id
            await client.interrupt()

    asyncio.run(drive())
    steer = next(
        message for message in read_log(log_path) if message.get("method") == "turn/steer"
    )
    assert steer["params"] == {
        "threadId": "codex-thread-1",
        "expectedTurnId": "turn-1",
        "input": [{"type": "text", "text": "follow up"}],
    }


def test_steer_is_native_and_never_starts_or_interrupts_a_turn(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "wire.jsonl"

    async def drive() -> None:
        client = CodexAppServerClient(
            spec(),
            command=(sys.executable, str(FAKE_CODEX)),
            env={"FAKE_CODEX_MODE": "hold", "FAKE_CODEX_LOG": str(log_path)},
        )
        async with client:
            await client.query("first")
            assert await client.steer("hold the old name") == "steered"
            await client.interrupt()

    asyncio.run(drive())
    methods = [message.get("method") for message in read_log(log_path)]
    assert methods.count("turn/steer") == 1
    assert methods.count("turn/start") == 1
    assert methods.count("turn/interrupt") == 1


def test_steer_stale_expected_turn_is_a_safe_reject(tmp_path: Path) -> None:
    log_path = tmp_path / "wire.jsonl"

    async def drive() -> None:
        client = CodexAppServerClient(
            spec(),
            command=(sys.executable, str(FAKE_CODEX)),
            env={
                "FAKE_CODEX_MODE": "hold",
                "FAKE_CODEX_LOG": str(log_path),
                "FAKE_CODEX_STEER_MODE": "stale",
            },
        )
        async with client:
            await client.query("first")
            with pytest.raises(_SteerRejected):
                await client.steer("hold the old name")
            await client.interrupt()

    asyncio.run(drive())
    steer = next(
        message
        for message in read_log(log_path)
        if message.get("method") == "turn/steer"
    )
    assert steer["params"]["expectedTurnId"] == "turn-1"


def test_steer_write_then_silence_is_unknown_not_accepted(tmp_path: Path) -> None:
    log_path = tmp_path / "wire.jsonl"

    async def drive() -> None:
        client = CodexAppServerClient(
            spec(),
            command=(sys.executable, str(FAKE_CODEX)),
            env={
                "FAKE_CODEX_MODE": "hold",
                "FAKE_CODEX_LOG": str(log_path),
                "FAKE_CODEX_STEER_MODE": "silent",
            },
        )
        async with client:
            await client.query("first")
            with pytest.raises(_SteerUnknown):
                await client.steer("hold the old name", timeout=0.2)
            await client.interrupt()

    asyncio.run(drive())


def test_steer_different_returned_turn_is_unknown_not_accepted(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "wire.jsonl"

    async def drive() -> None:
        client = CodexAppServerClient(
            spec(),
            command=(sys.executable, str(FAKE_CODEX)),
            env={
                "FAKE_CODEX_MODE": "hold",
                "FAKE_CODEX_LOG": str(log_path),
                "FAKE_CODEX_STEER_MODE": "different",
            },
        )
        async with client:
            await client.query("first")
            with pytest.raises(_SteerUnknown):
                await client.steer("hold the old name")
            await client.interrupt()

    asyncio.run(drive())


def test_steer_idle_is_a_safe_reject_without_wire_writes(tmp_path: Path) -> None:
    log_path = tmp_path / "wire.jsonl"

    async def drive() -> None:
        client = CodexAppServerClient(
            spec(),
            command=(sys.executable, str(FAKE_CODEX)),
            env={"FAKE_CODEX_MODE": "hold", "FAKE_CODEX_LOG": str(log_path)},
        )
        async with client:
            with pytest.raises(_SteerRejected):
                await client.steer("nobody is running")

    asyncio.run(drive())
    assert not any(
        message.get("method") == "turn/steer" for message in read_log(log_path)
    )


def test_failed_turn_and_missing_final_reply_fail_for_daemon_retry() -> None:
    for mode, expected in (("failed", "model turn failed"), ("no-final", "no final")):
        process = fake_process(mode)
        try:
            assert process.enqueue(delivery(f"delivery-{mode}", "work"))
            [result] = wait_for_results(process, 1)
            assert result.status is HarnessResultStatus.FAILED
            assert expected in (result.error or "")
        finally:
            process.stop()


def test_completion_poll_recovers_a_dropped_turn_notification() -> None:
    process = fake_process("no-notification")
    try:
        assert process.enqueue(delivery("delivery-poll", "poll me"))
        [result] = wait_for_results(process, 1)
        assert result.status is HarnessResultStatus.COMPLETED
        assert result.output == _prompt("poll me done")
        events = process.drain_progress()
        assert len(events) == 1
        assert events[0].phase == "turn-end"
        assert events[0].summary == "Codex turn completed"
        assert events[0].terminal is True
    finally:
        process.stop()


def test_unattended_client_answers_request_user_input_with_no_answers() -> None:
    """A blocking question must not be left open (schema requires answers)."""

    answer = _server_request_response(
        13,
        "item/tool/requestUserInput",
        {
            "threadId": "t",
            "turnId": "u",
            "itemId": "i",
            "questions": [{"id": "q", "header": "h", "question": "?"}],
            "isBlocking": True,
        },
    )
    assert answer == {"id": 13, "result": {"answers": {}}}
