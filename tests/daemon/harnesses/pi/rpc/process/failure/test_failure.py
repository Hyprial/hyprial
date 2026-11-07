from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from hyprial.daemon.impl.api import HarnessResultStatus
from hyprial.daemon.impl.harnesses.pi import rpc_client as pi_rpc
from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient

from ..support import (
    FAKE_PI,
    _reaped_process_with,
    delivery,
    fake_process,
    spec,
    wait_for_results,
)


def test_context_exhaustion_after_compaction_fails_without_another_retry(
    tmp_path: Path,
) -> None:
    command_log = tmp_path / "commands.jsonl"
    process = fake_process("context-exhausted-stuck", command_log=command_log)
    try:
        assert process.enqueue(delivery("delivery-context", "large turn"))
        [result] = wait_for_results(process, 1)

        assert result.status is HarnessResultStatus.FAILED
        assert result.failure_code == "CONTEXT_EXHAUSTED"
        commands = [
            json.loads(line) for line in command_log.read_text(encoding="utf-8").splitlines()
        ]
        assert [item["type"] for item in commands].count("compact") == 1
        assert [item["type"] for item in commands].count("prompt") == 2
    finally:
        process.stop()


def test_a_stdout_timeout_does_not_spawn_a_second_reader() -> None:
    """Two drains on one stream is a concurrent reader error.

    stdout_task was a local of _close(), so a shielded timeout left it running
    unrecorded and the next close started another one on the same stream.
    """

    class BlockingStdout:
        """_drain_stdout consumes via read(); block there, not in readline."""

        def __init__(self) -> None:
            self.readers = 0

        async def readline(self) -> bytes:
            return b""

        async def read(self, _n: int = -1) -> bytes:
            self.readers += 1
            if self.readers > 1:
                raise RuntimeError("concurrent stdout reader")
            await asyncio.Event().wait()
            return b""

    class EmptyStream:
        async def readline(self) -> bytes:
            return b""

        async def read(self, _n: int = -1) -> bytes:
            return b""

    async def scenario() -> None:
        stdout = BlockingStdout()
        client = PiRpcClient(
            spec(), "stdout-timeout", command=(sys.executable, str(FAKE_PI))
        )
        client._process = _reaped_process_with(stdout, EmptyStream())  # type: ignore[assignment]
        client._process_protocol = None
        client._stderr_task = None
        client._exit_task = None

        saved = pi_rpc._CLOSE_STEP_SECONDS, pi_rpc._CLOSE_TOTAL_SECONDS
        pi_rpc._CLOSE_STEP_SECONDS, pi_rpc._CLOSE_TOTAL_SECONDS = 0.05, 0.12
        try:
            await client._close()
            first = client._stdout_task
            assert first is not None and not first.done()

            # A repeated close must reuse the live drain, not start a rival.
            await client._close()
            assert client._stdout_task is first
            assert stdout.readers == 1
        finally:
            pi_rpc._CLOSE_STEP_SECONDS, pi_rpc._CLOSE_TOTAL_SECONDS = saved
            if client._stdout_task is not None:
                client._stdout_task.cancel()

    asyncio.run(scenario())
