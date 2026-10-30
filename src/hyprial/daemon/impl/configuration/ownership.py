"""Exclusive ownership fence for daemon-managed state registries."""

from __future__ import annotations

import os
import time
from pathlib import Path
from collections.abc import Callable
from typing import IO

from hyprial.kernel import lock_exclusive


class DaemonOwnershipBusy(RuntimeError):
    code = "DAEMON_OWNERSHIP_BUSY"


class DaemonStateOwnershipFence:
    """One held ``daemon.lock`` flock, transferable to the daemon runtime."""

    def __init__(self, path: Path, stream: IO[str]) -> None:
        self.path = path
        self._stream: IO[str] | None = stream

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
        stream = path.open("a+", encoding="utf-8")
        os.chmod(path, 0o600)
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                lock_exclusive(stream.fileno(), blocking=False)
                return cls(path, stream)
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    stream.close()
                    raise DaemonOwnershipBusy(
                        "daemon state ownership is held by a running or "
                        "starting daemon"
                    ) from error
                time.sleep(max(0.001, poll_interval))

    def detach(self) -> IO[str]:
        stream = self._stream
        if stream is None:
            raise RuntimeError("daemon ownership fence is already detached")
        self._stream = None
        return stream

    def close(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.close()

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
