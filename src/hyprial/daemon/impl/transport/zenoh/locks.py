"""Transport lock primitives with holder diagnostics."""
from __future__ import annotations

import sys
import threading
import time
import traceback
from typing import Any




class TransportLockTimeout(RuntimeError):
    """A bounded wait for the transport lock ran out; names the holder."""

def _thread_stack(ident: int | None) -> str:
    frame = sys._current_frames().get(ident) if ident is not None else None
    if frame is None:
        return "<no Python frame>"
    return "".join(traceback.format_stack(frame))

class _OwnedLock:
    """An RLock that remembers which thread holds it and since when.

    The 2026-09-27 stall could not answer "who holds the lock": faulthandler
    stops at 100 threads and py-spy needs root on macOS.  Recording the owner
    on first acquire makes the answer one attribute read away, and the
    ``_release_save``/``_acquire_restore`` pair keeps that record correct
    while a ``threading.Condition`` waits on this lock.
    """

    def __init__(self) -> None:
        self._inner = threading.RLock()
        self._depth = 0
        self._owner: tuple[int | None, str, float] | None = None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        acquired = self._inner.acquire(blocking, timeout)
        if acquired:
            self._depth += 1
            if self._depth == 1:
                self._claim()
        return acquired

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
        self._inner.release()

    __enter__ = acquire

    def __exit__(self, *_: object) -> None:
        self.release()

    def _at_fork_reinit(self) -> None:
        self._inner._at_fork_reinit()  # type: ignore[attr-defined]
        self._depth = 0
        self._owner = None

    def _claim(self) -> None:
        current = threading.current_thread()
        self._owner = (current.ident, current.name, time.monotonic())

    def locked(self) -> bool:
        # Python 3.14's threading.Condition binds ``lock.locked`` at construction;
        # without it the daemon fails at startup.  RLock.locked() is 3.14-only.
        return self._depth > 0

    # Condition protocol: a wait fully releases a reentrant hold and restores it.
    def _is_owned(self) -> bool:
        return self._inner._is_owned()  # type: ignore[attr-defined]

    def _release_save(self) -> Any:
        depth = self._depth
        self._depth = 0
        self._owner = None
        return self._inner._release_save(), depth  # type: ignore[attr-defined]

    def _acquire_restore(self, state: Any) -> None:
        inner_state, depth = state
        self._inner._acquire_restore(inner_state)  # type: ignore[attr-defined]
        self._depth = depth
        self._claim()

    def owner(self) -> tuple[int | None, str, float] | None:
        return self._owner
