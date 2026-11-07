"""``ProcessFileLock``: kernel release on holder death, in-process probe, busy."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hyprial.kernel import FileLockBusy, ProcessFileLock

_ROOT = Path(__file__).resolve().parents[3]
_HOLDER = """
import sys, time
from hyprial.kernel import ProcessFileLock
lock = ProcessFileLock(sys.argv[1])
lock.acquire(5.0)
print("held", flush=True)
time.sleep(600)
"""


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    source = str(_ROOT / "src")
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source if not inherited else os.pathsep.join((source, inherited))
    )
    return environment


def test_holder_kill_releases_the_lock(tmp_path: Path) -> None:
    """SIGKILL / TerminateProcess: the OS releases it; no pid takeover logic."""

    path = tmp_path / "home.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path)],
        cwd=_ROOT,
        env=_environment(),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        probe = ProcessFileLock(path)
        assert probe.probe() == "held"
        with pytest.raises(FileLockBusy):
            probe.acquire(0.0)
        holder.kill()  # SIGKILL on POSIX, TerminateProcess on Windows
        holder.wait(timeout=30)
        # Windows unlocks asynchronously after termination: allow seconds.
        deadline = time.monotonic() + 10.0
        while probe.probe() != "free":
            assert time.monotonic() < deadline, "lock not released after holder kill"
            time.sleep(0.05)
        probe.acquire(5.0)
        assert probe.held
        probe.release()
        assert path.exists(), "preserve_lock_file keeps the path on release"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=30)


def test_probe_is_self_owned_while_this_process_holds_it(tmp_path: Path) -> None:
    path = tmp_path / "home.lock"
    owner = ProcessFileLock(path)
    other = ProcessFileLock(path)
    assert other.probe() == "free"
    owner.acquire(1.0)
    try:
        # A try-lock from a second instance would either re-enter or report
        # "held" by someone else; the wrapper answers the truth.
        assert other.probe() == "self-owned"
        assert owner.probe() == "self-owned"
        with pytest.raises(FileLockBusy):
            other.acquire(0.0)
        with pytest.raises(FileLockBusy):
            other.acquire(0.1)
    finally:
        owner.release()
    assert other.probe() == "free"


def test_release_from_another_thread(tmp_path: Path) -> None:
    import threading

    lock = ProcessFileLock(tmp_path / "home.lock")
    lock.acquire(1.0)
    thread = threading.Thread(target=lock.release)
    thread.start()
    thread.join(timeout=10)
    assert not lock.held
    assert ProcessFileLock(tmp_path / "home.lock").probe() == "free"


def test_lock_is_not_inherited_by_a_child(tmp_path: Path) -> None:
    """A delegate child never keeps the owner's lock alive."""

    path = tmp_path / "home.lock"
    lock = ProcessFileLock(path)
    lock.acquire(1.0)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        close_fds=False,
    )
    try:
        lock.release()
        assert ProcessFileLock(path).probe() == "free"
    finally:
        child.kill()
        child.wait(timeout=30)
