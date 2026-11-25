"""The command that starts the daemon waits for it, so an exit leaves no zombie.

Platform independent: a reaped child has its ``returncode`` set without anyone
polling it, an unreaped one keeps ``None`` until polled.  The waiter must not
signal the child, must be a daemon thread, and only ends with the child.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

from hyprial.shell.impl.cli.commands.common.daemon_start import _reap_when_it_exits


def _reapers() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "daemon-reaper"]


def test_an_exited_child_is_reaped_without_anyone_polling_it():
    child = subprocess.Popen([sys.executable, "-c", "import sys, time; time.sleep(0.5); sys.exit(7)"])
    try:
        _reap_when_it_exits(child)
        waiter = _reapers()[-1]
        assert waiter.daemon is True
        assert child.returncode is None and waiter.is_alive()
        deadline = time.monotonic() + 30
        while child.returncode is None:
            assert time.monotonic() < deadline, "the child was never reaped"
            time.sleep(0.02)
        assert child.returncode == 7
        waiter.join(10)
        assert not waiter.is_alive()
    finally:
        child.kill() if child.poll() is None else None
        child.wait(timeout=30)


def test_waiting_never_stops_a_running_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _reap_when_it_exits(child)
        waiter = _reapers()[-1]
        time.sleep(0.3)
        assert child.poll() is None and waiter.is_alive()
    finally:
        child.kill()
        child.wait(timeout=30)
        waiter.join(10)
