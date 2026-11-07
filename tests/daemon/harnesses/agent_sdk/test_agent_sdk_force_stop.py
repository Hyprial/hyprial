"""Card 104164aa (a): the Claude SDK harness force-stops its worker group.

A worker whose graceful stop protocol cannot complete -- it is still resolving
its ``uv`` environment, or ignores the ``stop`` op -- must be reaped by PID
group instead of leaving the wrapper alive-but-unstoppable.  Without a
force-stop hook ``StreamingTurnProcess.stop`` raises after its join timeout,
which the lifecycle remove effect turns into an unbounded re-drive (the hang
this card reproduces).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator

import pytest

from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.agent_sdk import ClaudeAgentSdkProcess, IsolatedAgentSdkClient
from hyprial.daemon.impl.harnesses.codex.process import PROCESS_FORCE_JOIN_SECONDS, _OwnedProcessGroup

# Emits the ready handshake, then exits cleanly when told to stop -- the normal
# path, used to prove the client owns and registers its worker group without
# leaking the asyncio subprocess pipes.
_READY_WORKER = (
    "import sys, json\n"
    "sys.stdout.write(json.dumps({'type': 'ready'}) + '\\n')\n"
    "sys.stdout.flush()\n"
    "for line in sys.stdin:\n"
    "    if json.loads(line).get('op') == 'stop':\n"
    "        break\n"
)


class _IdleClient:
    """In-memory TurnClient that connects instantly and never spawns a worker."""

    async def __aenter__(self) -> "_IdleClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def query(self, prompt: str) -> None:
        return None

    async def receive_response(self) -> AsyncIterator[object]:
        if False:
            yield None

    async def interrupt(self) -> None:
        return None


def _spec() -> HarnessLaunchSpec:
    return HarnessLaunchSpec(harness="claude", name="cc", headless=True, cwd="/work")


def test_claude_wrapper_wires_force_stop_to_its_process_group() -> None:
    # Drive the pump with an in-memory client so no real worker is spawned; the
    # wiring under test is entirely in the constructor.
    proc = ClaudeAgentSdkProcess(_spec(), client_factory=_IdleClient)
    try:
        assert isinstance(proc._process_group, _OwnedProcessGroup)
        # The base pump can force-kill by PID group, not merely raise on a
        # stuck graceful stop.
        assert proc._force_stop == proc._process_group.force_close
        assert proc._force_stopped == proc._process_group.stopped
        assert proc._force_stop_join_seconds == PROCESS_FORCE_JOIN_SECONDS
    finally:
        proc.stop()


@pytest.mark.anyio
async def test_isolated_client_registers_its_own_worker_group() -> None:
    group = _OwnedProcessGroup()
    client = IsolatedAgentSdkClient(
        _spec(),
        worker_command=(sys.executable, "-c", _READY_WORKER),
        startup_timeout_seconds=5.0,
        process_group=group,
    )
    async with client:
        pid = client.pid
        assert pid is not None
        # Spawned with start_new_session, ownership published on entry: the
        # worker sits in its own group, killable by PGID without the daemon.
        assert group.exists(pid)
    # The graceful exit reaps the worker and releases custody.
    assert group.stopped()


def test_owned_group_force_close_reaps_a_session_leader() -> None:
    # The force path the Claude harness now relies on: a worker in its own
    # session is drained whole by PID group even when it never cooperates.
    group = _OwnedProcessGroup()
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time\nwhile True:\n    time.sleep(0.05)\n"],
        start_new_session=True,
    )
    try:
        group.register(worker.pid)
        assert group.exists(worker.pid)
        group.force_close()
        # Reap the killed leader so the group holds no zombie remnant before
        # asserting it is drained.
        worker.wait(timeout=2.0)
        assert group.stopped()
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=2.0)
