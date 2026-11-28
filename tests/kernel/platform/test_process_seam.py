"""The one synchronous GUI child spawn/terminate seam.

Decision: gui-lifecycle-process-seam-20261107.  The GUI tree may not branch on
the platform, so these two helpers are the only place its process-group
behaviour lives.  The cases below pin both platforms without starting or killing
a real process: ``os.killpg``, ``subprocess.run`` and the process handle are all
fakes.
Decision (2026-11-26): the POSIX cases fake the POSIX branch completely, so
they run on any host: ``os.killpg`` is patched with ``raising=False`` (Windows
has no such attribute) and ``_posix`` swaps the seam's ``signal`` module for
``POSIX_SIGNALS``, whose ``SIGKILL`` is 9 where the host has none.
"""

from __future__ import annotations

import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hyprial.kernel.impl.manysys.process import spawn as seam

POSIX_SIGNALS = SimpleNamespace(
    SIGINT=signal.SIGINT,
    SIGTERM=signal.SIGTERM,
    SIGKILL=getattr(signal, "SIGKILL", 9),
)


class FakeProcess:
    """A process handle whose liveness the test drives explicitly."""

    def __init__(self, *, alive_until: int | None = None) -> None:
        self.pid = 4321
        self.poll_calls = 0
        self.alive_until = alive_until
        self.terminated = 0
        self.killed = 0
        self.waited: list[float] = []
        self.wait_error: BaseException | None = None

    def poll(self):
        self.poll_calls += 1
        if self.alive_until is not None and self.poll_calls >= self.alive_until:
            return 0
        return None

    def terminate(self) -> None:
        self.terminated += 1

    def kill(self) -> None:
        self.killed += 1

    def wait(self, timeout: float | None = None):
        self.waited.append(timeout)
        if self.wait_error is not None:
            raise self.wait_error
        return 0


def test_spawn_kwargs_are_the_single_platform_switch(monkeypatch):
    # ``CREATE_NEW_PROCESS_GROUP`` only exists on Windows; the value is the
    # documented flag bit, and the helper must use the real attribute there.
    monkeypatch.setattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False
    )
    monkeypatch.setattr(sys, "platform", "win32")
    assert seam.spawn_kwargs() == {"creationflags": 0x00000200}
    monkeypatch.setattr(sys, "platform", "darwin")
    assert seam.spawn_kwargs() == {"start_new_session": True}
    monkeypatch.setattr(sys, "platform", "linux")
    assert seam.spawn_kwargs() == {"start_new_session": True}


def test_hidden_subprocess_kwargs_are_windows_only(monkeypatch):
    monkeypatch.setattr(
        subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False
    )
    monkeypatch.setattr(sys, "platform", "win32")
    assert seam.hidden_subprocess_kwargs() == {"creationflags": 0x08000000}
    monkeypatch.setattr(sys, "platform", "darwin")
    assert seam.hidden_subprocess_kwargs() == {}


def _posix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(seam, "signal", POSIX_SIGNALS)


def test_group_stop_escalates_int_then_term(monkeypatch):
    _posix(monkeypatch)
    signals: list[int] = []
    alive = {True}

    def killpg(group, signal_number):
        signals.append(signal_number)
        if signal_number == seam.signal.SIGTERM:
            alive.clear()

    monkeypatch.setattr(seam.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(seam, "_group_alive", lambda _group: bool(alive))
    process = FakeProcess()

    seam.terminate_process_tree(process, 99, stop_timeout=0.01, kill_timeout=0.01)

    assert signals == [seam.signal.SIGINT, seam.signal.SIGTERM]


def test_group_stop_reaches_sigkill_when_nothing_else_lands(monkeypatch):
    _posix(monkeypatch)
    signals: list[int] = []

    def killpg(group, signal_number):
        signals.append(signal_number)
        if signal_number == seam.signal.SIGKILL:
            raise ProcessLookupError

    monkeypatch.setattr(seam.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(seam, "_group_alive", lambda _group: True)
    process = FakeProcess()

    seam.terminate_process_tree(process, 99, stop_timeout=0.001, kill_timeout=0.001)

    assert signals == [
        seam.signal.SIGINT,
        seam.signal.SIGTERM,
        seam.signal.SIGKILL,
    ]


def test_group_stop_stops_when_the_group_is_already_gone(monkeypatch):
    _posix(monkeypatch)
    calls: list[int] = []
    monkeypatch.setattr(seam.os, "killpg", lambda *args: calls.append(args), raising=False)
    monkeypatch.setattr(seam, "_group_alive", lambda _group: False)
    process = FakeProcess()

    seam.terminate_process_tree(process, 99, stop_timeout=1, kill_timeout=1)

    assert calls == []
    assert process.poll_calls == 1


def test_group_stop_falls_back_to_the_handle_on_killpg_error(monkeypatch):
    _posix(monkeypatch)
    monkeypatch.setattr(
        seam.os, "killpg", MagicMock(side_effect=OSError("no such group")), raising=False
    )
    monkeypatch.setattr(seam, "_group_alive", lambda _group: True)
    process = FakeProcess()

    seam.terminate_process_tree(process, 99, stop_timeout=0.01, kill_timeout=0.5)

    assert process.terminated == 1
    assert process.waited == [0.5]


def test_group_stop_returns_on_process_lookup(monkeypatch):
    _posix(monkeypatch)
    monkeypatch.setattr(
        seam.os, "killpg", MagicMock(side_effect=ProcessLookupError), raising=False
    )
    monkeypatch.setattr(seam, "_group_alive", lambda _group: True)
    process = FakeProcess()

    seam.terminate_process_tree(process, 99, stop_timeout=0.01, kill_timeout=0.01)

    assert process.terminated == 0
    assert process.killed == 0


def test_group_none_falls_back_to_one_process(monkeypatch):
    _posix(monkeypatch)
    process = FakeProcess()

    seam.terminate_process_tree(process, None, stop_timeout=0.01, kill_timeout=0.5)

    assert process.terminated == 1
    assert process.waited == [0.5]
    assert process.killed == 0


def test_group_none_skips_an_already_exited_process(monkeypatch):
    _posix(monkeypatch)
    process = FakeProcess(alive_until=1)

    seam.terminate_process_tree(process, None, stop_timeout=0.01, kill_timeout=0.5)

    assert process.terminated == 0
    assert process.waited == []


@pytest.mark.parametrize("error", [OSError("gone"), AttributeError("no poll")])
def test_group_none_poll_failure_signals_nothing(monkeypatch, error):
    """The first poll of a group-less handle must not fall through to killing.

    ``poll`` raising means this handle cannot be observed; the previous
    implementation returned there instead of escalating, so the seam keeps that
    guard outside the termination fallback.
    """

    _posix(monkeypatch)
    process = FakeProcess()
    process.poll = MagicMock(side_effect=error)

    seam.terminate_process_tree(process, None, stop_timeout=0.01, kill_timeout=2.0)

    assert process.terminated == 0
    assert process.killed == 0
    assert process.waited == []


def test_group_none_poll_timeout_is_not_swallowed(monkeypatch):
    """``TimeoutExpired`` from ``poll`` is not an exit signal: it propagates."""

    _posix(monkeypatch)
    process = FakeProcess()
    process.poll = MagicMock(side_effect=subprocess.TimeoutExpired("cmd", 1))

    with pytest.raises(subprocess.TimeoutExpired):
        seam.terminate_process_tree(process, None, stop_timeout=0.01, kill_timeout=2.0)

    assert process.terminated == 0
    assert process.killed == 0
    assert process.waited == []


def test_group_none_escalates_to_kill_after_a_wait_timeout(monkeypatch):
    _posix(monkeypatch)
    process = FakeProcess()
    process.wait_error = subprocess.TimeoutExpired("cmd", 1)

    seam.terminate_process_tree(process, None, stop_timeout=0.01, kill_timeout=0.5)

    assert process.terminated == 1
    assert process.killed == 1
    assert process.waited == [0.5, 0.5]


def test_windows_stop_asks_taskkill_for_the_tree_then_waits(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    run = MagicMock()
    monkeypatch.setattr(seam.subprocess, "run", run)
    process = FakeProcess()

    seam.terminate_process_tree(process, None, stop_timeout=7.0, kill_timeout=3.0)

    run.assert_called_once_with(
        ["taskkill", "/PID", "4321", "/T", "/F"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=7.0,
    )
    assert process.waited == [3.0]
    assert process.terminated == 0


def test_windows_stop_keeps_going_when_taskkill_fails(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        seam.subprocess,
        "run",
        MagicMock(side_effect=OSError("taskkill missing")),
    )
    process = FakeProcess()

    seam.terminate_process_tree(process, 99, stop_timeout=1.0, kill_timeout=2.0)

    assert process.waited == [2.0]


def test_windows_stop_terminates_after_a_wait_timeout(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(seam.subprocess, "run", MagicMock())
    process = FakeProcess()
    process.wait_error = subprocess.TimeoutExpired("cmd", 1)

    seam.terminate_process_tree(process, None, stop_timeout=1.0, kill_timeout=2.0)

    assert process.terminated == 1


def test_windows_stop_skips_an_already_exited_process(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    run = MagicMock()
    monkeypatch.setattr(seam.subprocess, "run", run)
    process = FakeProcess(alive_until=1)

    seam.terminate_process_tree(process, None, stop_timeout=1.0, kill_timeout=2.0)

    run.assert_not_called()


def test_windows_stop_tolerates_a_handle_without_poll(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    run = MagicMock()
    monkeypatch.setattr(seam.subprocess, "run", run)
    process = SimpleNamespace(pid=4321)

    seam.terminate_process_tree(process, None, stop_timeout=1.0, kill_timeout=2.0)

    run.assert_not_called()


@pytest.mark.parametrize("error", [ProcessLookupError(), PermissionError(), OSError()])
def test_group_probe_maps_errors_to_liveness(monkeypatch, error):
    monkeypatch.setattr(seam.os, "killpg", MagicMock(side_effect=error), raising=False)

    alive = seam._group_alive(99)

    assert alive is isinstance(error, PermissionError)
