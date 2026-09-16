"""Exclusive ownership fence for daemon-managed state registries."""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path
from typing import IO


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
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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
