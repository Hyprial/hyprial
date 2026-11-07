"""Startup-failure cleanup: the exec->probe window must not leak.

Decision record (shard3 triage, run 22330): a child can exit between spawn
and the ready-probe write.  The steady-state write path named that "session
is not running" and the startup branch did not guarantee the three pipes,
the stderr/exit tasks and the process wrapper's auxiliary threads were
released.  These cases inject the exit deterministically (the probe write
runs only after ``wait()``) instead of relying on load.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient
from hyprial.daemon.impl.harnesses.pi.rpc_process import PiRpcProcess

from ..support import FAKE_PI, _owned_pipe_fds, spec


def _fd_count() -> int:
    """Open file descriptors in this process, portable across the CI hosts."""

    directory = "/dev/fd" if Path("/dev/fd").exists() else "/proc/self/fd"
    return len(os.listdir(directory))


def _exited_before_write_client() -> PiRpcClient:
    client = PiRpcClient(
        spec(),
        "dead-session",
        command=(sys.executable, str(FAKE_PI)),
        env={
            "FAKE_PI_MODE": "reject-session",
            "FAKE_PI_DEAD_ID": "dead-session",
        },
        startup_timeout_seconds=2.0,
    )
    return client


def _write_only_after_child_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deterministic exec->write race: the child is reaped before the probe."""

    real_write = PiRpcClient._write

    async def write_after_exit(self, message):  # type: ignore[no-untyped-def]
        process = self._process
        assert process is not None
        await asyncio.wait_for(process.wait(), 2.0)
        return await real_write(self, message)

    monkeypatch.setattr(PiRpcClient, "_write", write_after_exit)


def test_ready_probe_failure_reports_a_child_that_exits_before_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup names the dead child, never the steady-state "not running".

    Injection: reject-session exits immediately; the probe write runs only
    after the child is reaped.  The old code classified this as "session is
    not running" because it checked returncode inside the write path.
    """

    client = _exited_before_write_client()
    _write_only_after_child_exit(monkeypatch)

    async def scenario() -> ConnectionError:
        with pytest.raises(ConnectionError) as raised:
            await client.__aenter__()
        return raised.value

    error = asyncio.run(scenario())

    assert "exited unexpectedly" in str(error), str(error)
    assert "cannot open session file" in str(error), str(error)
    assert client._process is None
    assert client._process_protocol is None
    assert client._stderr_task is None
    assert client._exit_task is None


def test_ready_probe_failure_closes_pipes_tasks_and_auxiliary_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed __aenter__ is a full resource boundary, not a reference drop.

    Pins the three child pipes, the stderr/exit tasks and any observer/relay
    thread: every one must be gone after the startup failure returns.
    """

    client = _exited_before_write_client()
    _write_only_after_child_exit(monkeypatch)
    before_threads = set(threading.enumerate())
    before_fds = _fd_count()

    async def scenario():
        task = asyncio.create_task(client.__aenter__())
        await asyncio.to_thread(client._spawned.wait, 2.0)
        pipes = _owned_pipe_fds(client)
        with pytest.raises(ConnectionError, match="exited unexpectedly"):
            await task
        pending = [
            pending for pending in asyncio.all_tasks() if pending is not asyncio.current_task()
        ]
        return pipes, pending

    pipes, pending = asyncio.run(scenario())

    for fd in pipes:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert client._process is None
    assert client._process_protocol is None
    assert client._stderr_task is None
    assert client._exit_task is None
    assert _fd_count() == before_fds
    assert pending == []
    leaked = [
        thread.name
        for thread in threading.enumerate()
        if thread not in before_threads
        and thread.name.startswith(("hyprial-pi-rpc", "hyprial-turn", "_RuntimeActor"))
    ]
    assert leaked == []


def test_failed_process_start_reclaims_its_threads_after_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The process wrapper must not outlive a failed start once stopped."""

    _write_only_after_child_exit(monkeypatch)
    launch = spec(session_ref="stable-session")
    before = set(threading.enumerate())
    process = PiRpcProcess(
        launch,
        client_factory=lambda: PiRpcClient(
            launch,
            "dead-session",
            command=(sys.executable, str(FAKE_PI)),
            env={
                "FAKE_PI_MODE": "reject-session",
                "FAKE_PI_DEAD_ID": "dead-session",
            },
            startup_timeout_seconds=2.0,
        ),
        reconnect_delay_seconds=0.01,
    )
    process.stop()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        leaked = [
            thread.name
            for thread in threading.enumerate()
            if thread not in before
            and thread.name.startswith(
                ("hyprial-pi-rpc", "hyprial-turn", "_RuntimeActor", "failed-start")
            )
        ]
        if not leaked:
            break
        time.sleep(0.01)
    assert not process._thread.is_alive()
    assert leaked == []
