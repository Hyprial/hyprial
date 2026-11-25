"""The portable process ledger finds descendants through ``ps``, refuses a reused pid and reaps.

Decision: portable-process-cleanup-20261119.  A child starts a grandchild; the
ledger finds both from ``ps`` alone, a ledger holding a different birth for the
grandchild's pid refuses to signal it (the process stays alive), and the ledger
that recorded the real births stops both and reports no survivor.  ``exists``
distinguishes "no such process" from a zombie, so a "gone" assertion is not true
by default on a host without ``/proc``.  Every wait is a bounded condition.
Decision: portable-process-cleanup-verify-20261119.  The child leads its own session
and its process group is killed unconditionally afterwards, so a failure before the
grandchild is recorded cannot leak it.  A zombie exists but is not alive.  A pid that
is alive while its birth cannot be verified is reported as a survivor and never
signalled; ``record`` without a birth keeps the birth a pid already has.  The
``GuiCommand`` and ``LifecycleHome`` cases plant a different live process under a
reaped launcher's pid in a fake table and require that it is never adopted or signalled,
that ``release`` is idempotent, and that a survivor fails the teardown with its pid and command.
"""

from __future__ import annotations

import contextlib
import os
import select
import signal
import subprocess
import sys
from collections.abc import Iterator

import pytest

from tests.integration.daemon_lifecycle.support import LifecycleHome
from tests.integration.gui_daemon_first.support import GuiCommand
from tests.support import process_table
from tests.support.process_table import Ledger, Row, alive, children_of, descendants_of, exists, table, wait_until

PARENT = r'''
import subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(grandchild.pid, flush=True)
time.sleep(120)
'''


def _first_line(child: subprocess.Popen, seconds: float = 30.0) -> str:
    assert child.stdout is not None
    ready, _, _ = select.select([child.stdout], [], [], seconds)
    assert ready, f"the child printed no grandchild pid within {seconds}s"
    return child.stdout.readline().strip()


@contextlib.contextmanager
def _leader() -> Iterator[tuple[subprocess.Popen, Ledger]]:
    child = subprocess.Popen(
        [sys.executable, "-c", PARENT], stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    ledger = Ledger()
    try:
        ledger.record(child.pid)
        yield child, ledger
    finally:
        if child.returncode is None and ledger.exists(child.pid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=30)
        if child.stdout is not None:
            child.stdout.close()


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen(["/bin/sleep", "120"], start_new_session=True, stdin=subprocess.DEVNULL)


def _end(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=30)


def test_the_ledger_finds_a_grandchild_refuses_a_reused_pid_and_reaps_both() -> None:
    with _leader() as (child, ledger):
        assert ledger.record(child.pid)
        grandchild = int(_first_line(child))
        assert wait_until(lambda: grandchild in children_of(child.pid), 30)
        assert descendants_of(child.pid) == [grandchild]
        assert table()[grandchild].ppid == child.pid
        assert ledger.adopt_descendants(child.pid) == [grandchild]
        assert ledger.live() == sorted([child.pid, grandchild])

        wrong = Ledger()
        assert wrong.record(grandchild, "not-the-birth-of-this-process")
        assert wrong.same(grandchild) is False
        assert wrong.signal(grandchild, signal.SIGKILL) is False
        assert Ledger().signal(grandchild, signal.SIGKILL) is False
        assert alive(grandchild) and ledger.same(grandchild)

        assert ledger.stop(term=10, kill=10) == []
        child.wait(timeout=10)
        assert child.returncode == -signal.SIGTERM
        assert not alive(grandchild) and not alive(child.pid)
        assert not ledger.same(grandchild)
        assert not ledger.exists(child.pid)
    assert ledger.live() == []


def test_a_failure_before_the_grandchild_is_recorded_leaves_nothing_behind() -> None:
    grandchild = 0
    with pytest.raises(RuntimeError, match="failed early"):
        with _leader() as (child, ledger):
            grandchild = int(_first_line(child))
            assert wait_until(lambda: grandchild in children_of(child.pid), 30)
            raise RuntimeError("failed early")
    # An orphaned zombie may remain in a container whose PID 1 does not reap it.
    # The cleanup contract is that the grandchild cannot keep running.
    assert wait_until(lambda: not alive(grandchild), 30)
    assert not ledger.live()


def test_an_exited_child_that_was_not_waited_exists_but_is_not_alive() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    ledger = Ledger()
    try:
        assert ledger.record(child.pid)
        assert child.stdin is not None
        child.stdin.close()
        assert wait_until(lambda: not alive(child.pid), 30)
        assert exists(child.pid) and ledger.exists(child.pid)
        assert not alive(child.pid) and not ledger.same(child.pid)
        child.wait(timeout=30)
        assert not exists(child.pid) and not ledger.exists(child.pid)
    finally:
        _end(child)


def test_an_alive_pid_whose_birth_cannot_be_read_is_a_survivor_that_is_not_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    victim = _sleeper()
    try:
        ledger = Ledger()
        assert ledger.record(victim.pid)
        monkeypatch.setattr(process_table, "process_birth_identity", lambda pid: None)
        assert ledger.live() == [victim.pid]
        assert ledger.signal(victim.pid, signal.SIGKILL) is False
        assert ledger.stop(term=0.2, kill=0.2) == [victim.pid]
        assert victim.poll() is None
        message = ledger.describe([victim.pid])
        assert f"pid {victim.pid}" in message and "/bin/sleep 120" in message and "alive, birth unverifiable" in message
    finally:
        _end(victim)


def test_a_supplied_birth_that_does_not_match_is_a_survivor_but_a_live_read_mismatch_is_a_reused_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    victim = _sleeper()
    try:
        supplied = Ledger()
        assert supplied.record(victim.pid, "a birth formatted some other way")
        assert supplied.live() == [victim.pid] and supplied.signal(victim.pid, signal.SIGKILL) is False
        assert "birth unverifiable" in supplied.describe([victim.pid])

        read = Ledger()
        assert read.record(victim.pid)
        monkeypatch.setattr(process_table, "process_birth_identity", lambda pid: "the birth of a reused pid")
        assert read.live() == [] and read.signal(victim.pid, signal.SIGKILL) is False
        assert victim.poll() is None
    finally:
        _end(victim)


def test_recording_without_a_birth_keeps_the_birth_a_pid_already_has(monkeypatch: pytest.MonkeyPatch) -> None:
    victim = _sleeper()
    try:
        ledger = Ledger()
        assert ledger.record(victim.pid)
        original = set(ledger.births[victim.pid])
        monkeypatch.setattr(process_table, "process_birth_identity", lambda pid: "the birth of a reused pid")
        assert ledger.record(victim.pid) is True
        assert ledger.births[victim.pid] == original
        assert ledger.same(victim.pid) is False
        assert ledger.signal(victim.pid, signal.SIGKILL) is False
        assert victim.poll() is None
    finally:
        _end(victim)


def _planted(monkeypatch: pytest.MonkeyPatch, launcher: int, victim: int) -> None:
    fake = {victim: Row(victim, launcher, "S", "/bin/sleep 120")}
    monkeypatch.setattr(process_table, "table", lambda: fake)


@pytest.mark.parametrize("rebirth", [False, True], ids=["launcher-reaped", "launcher-pid-held-by-another-birth"])
def test_a_gui_command_never_adopts_or_signals_the_children_of_a_pid_that_is_no_longer_its_launcher(
    monkeypatch: pytest.MonkeyPatch, rebirth: bool,
) -> None:
    victim = _sleeper()
    launcher = subprocess.Popen(["/bin/sleep", "120"] if rebirth else ["/bin/true"], start_new_session=True, stdout=subprocess.PIPE)
    try:
        command = GuiCommand(launcher, lambda: ())
        if rebirth:
            command.ledger.births[launcher.pid] = {"the birth of the process that now holds this pid"}
        else:
            launcher.wait(timeout=30)
        _planted(monkeypatch, launcher.pid, victim.pid)
        command.snapshot()
        assert victim.pid not in command.ledger.births
        command.release()
        command.release()
        assert victim.pid not in command.ledger.births and victim.poll() is None and alive(victim.pid)
    finally:
        _end(launcher)
        _end(victim)


def test_gui_command_release_runs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = subprocess.Popen(["/bin/true"], stdout=subprocess.PIPE)
    calls: list[int] = []
    try:
        command = GuiCommand(launcher, lambda: ())
        monkeypatch.setattr(command.ledger, "stop", lambda **options: calls.append(1) or [])
        command.release()
        command.release()
        assert calls == [1]
    finally:
        _end(launcher)


def test_a_gui_command_survivor_fails_the_release_and_the_lifecycle_cleanup_with_pid_and_command(monkeypatch: pytest.MonkeyPatch) -> None:
    stubborn = _sleeper()
    launcher = subprocess.Popen(["/bin/true"], stdout=subprocess.PIPE)
    home = LifecycleHome()
    try:
        command = GuiCommand(launcher, lambda: ())
        monkeypatch.setattr(command.ledger, "stop", lambda **options: [stubborn.pid])
        with pytest.raises(AssertionError, match=rf"pid {stubborn.pid} \(/bin/sleep 120\)"):
            command.release()
        command.released = False
        home.releasers.append(command.release)
        with pytest.raises(AssertionError, match=rf"pid {stubborn.pid} \(/bin/sleep 120\)"):
            home.cleanup()
    finally:
        _end(launcher)
        _end(stubborn)


def test_the_lifecycle_cleanup_reports_every_failure_not_only_the_first() -> None:
    home = LifecycleHome()

    def first() -> None:
        raise AssertionError("the first releaser failed")

    def second() -> None:
        raise AssertionError("the second releaser failed")

    home.releasers.extend([first, second])
    with pytest.raises(ExceptionGroup) as raised:
        home.cleanup()
    assert sorted(str(error) for error in raised.value.exceptions) == ["the first releaser failed", "the second releaser failed"]
