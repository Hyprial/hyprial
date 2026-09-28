"""Exclusive locks for dedicated lock files (never data-file byte ranges).

POSIX keeps flock semantics. Windows locks byte zero using the CRT; closing
its descriptor releases ownership. This is NOT an inherited-flock handoff.
The caller owns the descriptor and must not concurrently seek/use it.
"""

from __future__ import annotations

import errno
import os
import time

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
