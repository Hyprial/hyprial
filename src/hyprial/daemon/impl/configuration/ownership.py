"""Exclusive ownership fence for daemon-managed state registries."""

from __future__ import annotations

import os
import time
from pathlib import Path
from collections.abc import Callable

from hyprial.kernel import FileLockBusy, ProcessFileLock


class DaemonOwnershipBusy(RuntimeError):
    code = "DAEMON_OWNERSHIP_BUSY"


class DaemonStateOwnershipFence:
    """One held ``daemon.lock``, movable to the in-process daemon runtime.

    Backed by the kernel :class:`ProcessFileLock`; it is never handed to
    another process (the next owner acquires after this one exits).
    """

    def __init__(self, path: Path, lock: ProcessFileLock) -> None:
        self.path = path
        self._lock: ProcessFileLock | None = lock

    @classmethod
    def acquire(
        cls,
        state_dir: Path,
        *,
        timeout: float = 0.0,
        poll_interval: float = 0.05,
    ) -> DaemonStateOwnershipFence:
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = state_dir / "daemon.lock"
        lock = ProcessFileLock(path)
        try:
            lock.acquire(timeout=timeout, poll_interval=poll_interval)
        except FileLockBusy as error:
            raise DaemonOwnershipBusy(
                "daemon state ownership is held by a running or "
                "starting daemon"
            ) from error
        try:
            os.chmod(path, 0o600)
        except BaseException:
            lock.release()
            raise
        return cls(path, lock)

    def detach(self) -> ProcessFileLock:
        lock = self._lock
        if lock is None:
            raise RuntimeError("daemon ownership fence is already detached")
        self._lock = None
        return lock

    def close(self) -> None:
        lock = self._lock
        self._lock = None
        if lock is not None:
            lock.release()

    def __enter__(self) -> DaemonStateOwnershipFence:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

def wait_for_state_ownership(
    state_dir: Path,
    *,
    acquire: Callable[[Path], DaemonStateOwnershipFence],
    probe_holder: Callable[[], int | None],
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[dict[str, object]], None] | None = None,
    poll_seconds: float = 1.0,
) -> DaemonStateOwnershipFence:
    """Take ownership, waiting while a healthy daemon still owns the state.

    Allen's ruling (2026-10-29): when the lock is held, the daemon blocks and
    takes over once the holder exits instead of exiting. Exiting is what made
    launchd's KeepAlive restart the same process 2,900 times: every attempt lost
    the race and reported failure.

    The wait is unbounded on purpose -- a hard deadline would just reinstate the
    restart loop -- but it is only taken while ``probe_holder`` proves a healthy
    daemon is serving. A lock held with no answering daemon stays a fast failure,
    because that is a broken state, not a busy one.
    """

    while True:
        try:
            return acquire(state_dir)
        except DaemonOwnershipBusy:
            holder = probe_holder()
            if holder is None:
                raise
            if log is not None:
                log({"event": "waiting-for-lock", "holder_pid": holder})
            sleep(max(0.001, poll_seconds))
