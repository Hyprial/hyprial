from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient
from hyprial.daemon.impl.harnesses.pi.rpc_process import PiRpcProcess

FAKE_PI = Path(__file__).resolve().parents[6] / 'tests/fixtures/harnesses/fake_pi_rpc.py'


def spec(*, args: tuple[str, ...] = (), session_ref: str | None = None) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness="pi",
        name="pi-worker",
        headless=True,
        args=args,
        session_ref=session_ref,
    )


def delivery(delivery_id: str, message: str) -> HarnessDelivery:
    return HarnessDelivery(
        delivery_id=delivery_id,
        conversation_id="conversation-1",
        sender="requester",
        recipient="agent:op:node-1:pi-worker",
        message=message,
    )


def _prompt(body: str) -> str:
    """Header plus ``body``: the exact prompt for a ``delivery()``; the fake echoes it + " done"."""
    return f"[Harness Network message from requester to agent:op:node-1:pi-worker]\n{body}"


def fake_process(
    mode: str,
    *,
    state_file: Path | None = None,
    command_log: Path | None = None,
    settle_grace_seconds: float = 1.5,
    startup_timeout_seconds: float = 10.0,
    extra_env: dict[str, str] | None = None,
) -> PiRpcProcess:
    environment = {"FAKE_PI_MODE": mode, **(extra_env or {})}
    if state_file is not None:
        environment["FAKE_PI_STATE"] = str(state_file)
    if command_log is not None:
        environment["FAKE_PI_LOG"] = str(command_log)
    launch = spec(session_ref="stable-session")
    return PiRpcProcess(
        launch,
        client_factory=lambda: PiRpcClient(
            launch,
            "stable-session",
            command=(sys.executable, str(FAKE_PI)),
            env=environment,
            startup_timeout_seconds=startup_timeout_seconds,
            settle_grace_seconds=settle_grace_seconds,
        ),
        reconnect_delay_seconds=0.01,
    )


def wait_for_results(process: PiRpcProcess, count: int, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    results = []
    while time.monotonic() < deadline:
        results.extend(process.drain_results())
        if len(results) >= count:
            return results
        time.sleep(0.01)
    pytest.fail(f"pi RPC pump produced {len(results)}/{count} results")








# --- pi --session-id charset gate (end-to-end shape) -------------------------
#
# Pi >=0.76 validates --session-id against [A-Za-z0-9._-] and exits at launch
# on any other character (colons included).  FAKE_PI_VALIDATE_SESSION_ID=1
# mirrors that gate; a colon-containing ref must therefore fail startup when
# the sanitization is removed (mutation guard) and pass after it.


































class SlowQueryPiRpcClient(PiRpcClient):
    """Hold open the selected-but-not-yet-active turn window."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.query_entered = threading.Event()

    async def query(self, prompt: str) -> None:
        self.query_entered.set()
        await asyncio.sleep(0.2)
        await super().query(prompt)








def _owned_pipe_fds(client: PiRpcClient) -> tuple[int, int, int]:
    protocol = client._process_protocol
    assert protocol is not None and protocol._transport is not None
    result: list[int] = []
    for pipe_fd in (0, 1, 2):
        transport = protocol._transport.get_pipe_transport(pipe_fd)
        assert transport is not None
        pipe = transport.get_extra_info("pipe")
        assert pipe is not None
        result.append(pipe.fileno())
    return result[0], result[1], result[2]
































def _reaped_process_with(stdout_stream, stderr_stream):
    class Reaped:
        returncode = 0
        stdin = None
        stdout = stdout_stream
        stderr = stderr_stream

        async def wait(self) -> int:
            return 0

        def terminate(self) -> None:  # pragma: no cover - already reaped
            raise AssertionError("already reaped")

        def kill(self) -> None:  # pragma: no cover - already reaped
            raise AssertionError("already reaped")

    return Reaped()






# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'FAKE_PI',
    'SlowQueryPiRpcClient',
    '_owned_pipe_fds',
    '_prompt',
    '_reaped_process_with',
    'delivery',
    'fake_process',
    'spec',
    'wait_for_results',
]
