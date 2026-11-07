"""Exclusive locks for dedicated lock files (never data-file byte ranges).

``lock_exclusive``/``unlock``: POSIX keeps flock semantics. Windows locks byte
zero using the CRT; closing its descriptor releases ownership. This is NOT an
inherited-flock handoff. The caller owns the descriptor and must not
concurrently seek/use it.

``ProcessFileLock``: the ``filelock``-backed lock for process-lifetime
ownership (identity transaction, daemon state fence).  It is never handed to
another process; the next owner acquires it after the previous one exits.
"""

from __future__ import annotations

import errno
import os
import threading
import time
from pathlib import Path
from typing import Literal

from filelock import FileLock, Timeout

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def lock_exclusive(descriptor: int, *, blocking: bool = True) -> None:
    if os.name != "nt":
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                return
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if not blocking:
                    raise BlockingIOError(errno.EAGAIN, "lock file is held") from error
                time.sleep(0.05)
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)


def unlock(descriptor: int) -> None:
    if os.name != "nt":
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)


# --- process-lifetime OS locks (filelock) ------------------------------------
#
# The one place ``filelock.FileLock`` is constructed (an architecture test
# forbids it elsewhere).  The library owns the platform code: ``fcntl.flock``
# on POSIX, ``LockFileEx`` on Windows -- one code path, no handle/descriptor
# handoff, released by the kernel when the holder exits (including SIGKILL /
# TerminateProcess).  The options are fixed here because the defaults are
# wrong for a lifetime lock:
#
# - ``fallback_to_soft=False``: fail closed on FUSE/NFS (ENOSYS) instead of
#   silently degrading to a marker file;
# - ``preserve_lock_file=True``: never unlink on release (Windows
#   delete-pending races; the path stays stable for every holder);
# - ``thread_local=False``: a lock taken on one thread may be released on
#   another (the daemon releases on its shutdown path);
# - ``is_singleton=False``: a second instance in this process must contend,
#   not silently re-enter; in-process ownership is answered by :meth:`probe`.

_HELD_IN_PROCESS: dict[str, int] = {}
_HELD_GUARD = threading.Lock()


class FileLockBusy(BlockingIOError):
    """Another holder owns the lock (raised instead of ``filelock.Timeout``)."""


class ProcessFileLock:
    """One exclusive OS lock on a dedicated lock file, owned by this process."""

    def __init__(self, path: Path, *, mode: int = 0o600) -> None:
        self.path = Path(path)
        self._key = os.path.normcase(os.path.abspath(self.path))
        self._lock = FileLock(
            str(self.path),
            mode=mode,
            thread_local=False,
            is_singleton=False,
            fallback_to_soft=False,
            preserve_lock_file=True,
        )
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self, timeout: float, *, poll_interval: float = 0.05) -> None:
        """Block up to ``timeout`` seconds (``0``: one attempt), else raise :class:`FileLockBusy`."""
        if self._held:
            raise RuntimeError(f"{self.path} is already held by this lock object")
        try:
            self._lock.acquire(
                timeout=max(0.0, timeout),
                poll_interval=max(0.001, poll_interval),
                blocking=timeout > 0,
            )
        except Timeout as error:
            raise FileLockBusy(errno.EAGAIN, f"lock file is held: {self.path}") from error
        self._held = True
        with _HELD_GUARD:
            _HELD_IN_PROCESS[self._key] = _HELD_IN_PROCESS.get(self._key, 0) + 1

    def probe(self) -> Literal["free", "held", "self-owned"]:
        """Who holds this path right now, without keeping it.

        Reserved API for the 0.5.5.4 home lifetime lock (CLI free/owned
        classification); in 0.5.5.3.4 only tests call it.

        ``self-owned`` when any lock object in this process holds the path (a
        try-lock from here would otherwise either re-enter or misreport).
        """
        with _HELD_GUARD:
            if _HELD_IN_PROCESS.get(self._key):
                return "self-owned"
        other = ProcessFileLock(self.path)
        try:
            other.acquire(0.0)
        except FileLockBusy:
            return "held"
        other.release()
        return "free"

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        with _HELD_GUARD:
            remaining = _HELD_IN_PROCESS.get(self._key, 0) - 1
            if remaining > 0:
                _HELD_IN_PROCESS[self._key] = remaining
            else:
                _HELD_IN_PROCESS.pop(self._key, None)
        self._lock.release(force=True)

    close = release

    def __enter__(self) -> ProcessFileLock:
        return self

    def __exit__(self, *_error: object) -> None:
        self.release()
