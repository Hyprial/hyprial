"""The session.register process fence against real process topologies.

The fence accepts exactly two shapes: the claimed process is the IPC peer's
direct child (codex's inverse shape), or the peer is an ancestor of the
claimed process (the older interactive carriers). The dsh-tui launcher used
to start the TUI and the carrier as siblings, which the fence refuses; the
carrier now owns the TUI, so the claimed TUI is the registration peer's direct
child.

These spawn real processes: fake pids would not exercise the ancestry or
parent reads the daemon performs. The PTY end-to-end run remains R&D's.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator

import pytest

from hyprial.daemon.impl.application.actors.sessions.registry import (
    _SessionRegistryMixin,
)
from hyprial.daemon.impl.mcp.channel.ownership import _read_process_identity
from hyprial.kernel import ipc_errors
from hyprial.kernel.impl.contracts.daemon.ipc_errors import DaemonRequestError

_SLEEPER = "import time; time.sleep(60)"


def _spawn() -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", _SLEEPER])


def _kill(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
        process.wait()


def _identity(pid: int) -> str:
    value = _read_process_identity(pid)
    assert value, f"no birth identity for live pid {pid}"
    return value


@pytest.fixture()
def children() -> Iterator[list[subprocess.Popen[bytes]]]:
    spawned: list[subprocess.Popen[bytes]] = []
    try:
        yield spawned
    finally:
        for process in spawned:
            _kill(process)


def _fence(claim: int, peer: int) -> tuple[int | None, str | None]:
    return _SessionRegistryMixin._verified_ipc_process_fence(
        claim, _identity(claim), peer
    )


def test_a_direct_child_claim_is_accepted(
    children: list[subprocess.Popen[bytes]],
) -> None:
    """The carrier-owned topology: the carrier spawned the claimed TUI."""

    script = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {_SLEEPER!r}])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    carrier = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
    )
    children.append(carrier)
    assert carrier.stdout is not None
    tui_pid = int(carrier.stdout.readline())

    assert _fence(tui_pid, carrier.pid) == (tui_pid, _identity(tui_pid))
    try:
        os.kill(tui_pid, 0)
    finally:
        os.kill(tui_pid, 15)


def test_a_sibling_claim_is_refused(
    children: list[subprocess.Popen[bytes]],
) -> None:
    """The old launcher topology: TUI and carrier are siblings; never accepted."""

    first = _spawn()
    second = _spawn()
    children.extend((first, second))

    with pytest.raises(DaemonRequestError) as refused:
        _fence(second.pid, first.pid)

    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_a_claim_with_a_foreign_identity_is_refused(
    children: list[subprocess.Popen[bytes]],
) -> None:
    tui = _spawn()
    children.append(tui)

    with pytest.raises(DaemonRequestError) as refused:
        _SessionRegistryMixin._verified_ipc_process_fence(
            tui.pid, "1:not-the-observed-birth-stamp", os.getpid()
        )

    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
