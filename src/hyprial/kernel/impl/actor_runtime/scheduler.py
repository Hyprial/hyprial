# Decision record (file header; no inline comments by repository rule):
# - Callbacks are fenced on the identity of the scheduled entry, not on the
#   generation value alone. Under the ABI v1 generation rule a restart that
#   fails (5.1 row 4b) and a reset (row 6) reschedule within the SAME
#   generation, so a superseded timer that already fired and is waiting for
#   the lock must not run the replacement's callback early (TMR-03).
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol


class TimerHandle(Protocol):
    daemon: bool

    def start(self) -> None: ...

    def cancel(self) -> None: ...


TimerFactory = Callable[..., TimerHandle]


class GenerationScheduler:
    """Schedules one generation-fenced callback per logical child."""

    def __init__(self, timer_factory: TimerFactory = threading.Timer) -> None:
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._entries: dict[str, tuple[int, TimerHandle, object]] = {}
        self._active_callbacks = 0
        self._closed = False

    def schedule(
        self,
        key: str,
        generation: int,
        delay: float,
        callback: Callable[[int], None],
    ) -> bool:
        with self._lock:
            if self._closed:
                return False
            previous = self._entries.pop(key, None)
            if previous is not None:
                previous[1].cancel()
            token = object()
            timer = self._timer_factory(
                delay,
                self._fire,
                args=(key, generation, callback, token),
            )
            timer.daemon = True
            self._entries[key] = (generation, timer, token)
            timer.start()
            return True

    def _fire(
        self,
        key: str,
        generation: int,
        callback: Callable[[int], None],
        token: object = None,
    ) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if (
                self._closed
                or entry is None
                or entry[0] != generation
                or entry[2] is not token
            ):
                return
            self._entries.pop(key, None)
            self._active_callbacks += 1
        try:
            callback(generation)
        finally:
            with self._condition:
                self._active_callbacks -= 1
                self._condition.notify_all()

    def cancel(self, key: str) -> bool:
        """Cancel the pending callback for ``key``; True if one was pending."""

        with self._lock:
            entry = self._entries.pop(key, None)
        if entry is not None:
            entry[1].cancel()
        return entry is not None

    def shutdown(self, timeout: float = 5.0) -> bool:
        """Cancel pending timers and wait a bounded time for active callbacks."""

        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._closed = True
            entries = tuple(self._entries.values())
            self._entries.clear()
        for _, timer, _ in entries:
            timer.cancel()
        with self._condition:
            while self._active_callbacks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True
