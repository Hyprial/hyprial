"""Notice a maintenance tick that does not come back, and say where it is.

``daemon.reconcile_overrun`` is written when a tick RETURNS late.  A tick
that never returns -- the 2026-09-27 stall, 40 minutes with nothing logged --
is invisible to it.  This watchdog is told when a tick starts, which phase it
enters and when it finishes, and a thread of its own checks those marks: a
tick running longer than the stall budget, or no tick started for that long,
is logged as ``daemon.maintenance.stalled`` with the phase, the tick thread's
Python stack and whatever the diagnostics hook reports (the transport lock
holder).  It repeats once per budget while the stall lasts and logs
``daemon.maintenance.recovered`` when the tick completes.

It observes only: it never interrupts or restarts the tick.
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

#: A tick normally takes well under a second and runs every second; one
#: minute without a completed tick is a stall, not a slow tick.
DEFAULT_STALL_SECONDS = 60.0
POLL_SECONDS = 5.0


def _stack_of(ident: int | None) -> str:
    frame = sys._current_frames().get(ident) if ident is not None else None
    if frame is None:
        return "<no Python frame>"
    return "".join(traceback.format_stack(frame))


class MaintenanceWatchdog:
    def __init__(
        self,
        *,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
        log: Callable[..., None],
        diagnostics: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stall_seconds = stall_seconds
        self._log = log
        self._diagnostics = diagnostics
        self._clock = clock
        self._guard = threading.Lock()
        # (thread ident, thread name, started) of the tick in flight.
        self._tick: tuple[int | None, str, float] | None = None
        self._phase = "start"
        self._last_finished: float | None = None
        self._reported_at: float | None = None
        self._stalled_since: float | None = None
        self._thread: threading.Thread | None = None

    def tick_started(self) -> None:
        current = threading.current_thread()
        with self._guard:
            self._tick = (current.ident, current.name, self._clock())
            self._phase = "start"

    def phase(self, name: str) -> None:
        self._phase = name

    def tick_finished(self) -> None:
        now = self._clock()
        with self._guard:
            tick = self._tick
            self._tick = None
            self._last_finished = now
            stalled_since = self._stalled_since
            self._stalled_since = None
            self._reported_at = None
            phase = self._phase
        if stalled_since is not None and tick is not None:
            self._log(
                "info",
                "daemon.maintenance.recovered",
                phase=phase,
                durationMs=int((now - tick[2]) * 1000),
            )

    def check(self, now: float | None = None) -> dict[str, Any] | None:
        """Log and return a stall report when one is due, else ``None``."""

        now = self._clock() if now is None else now
        with self._guard:
            tick = self._tick
            if tick is not None:
                ident, name, since = tick
                phase = self._phase
            elif self._last_finished is not None:
                ident, name, since = None, "", self._last_finished
                phase = "between-ticks"
            else:
                return None  # no tick has run yet: startup, not a stall
            elapsed = now - since
            if elapsed <= self._stall_seconds:
                return None
            if (
                self._reported_at is not None
                and now - self._reported_at < self._stall_seconds
            ):
                return None
            self._reported_at = now
            if self._stalled_since is None:
                self._stalled_since = since
        report: dict[str, Any] = {
            "phase": phase,
            "elapsedMs": int(elapsed * 1000),
            "thread": name,
            "stack": _stack_of(ident) if ident is not None else "",
            "transportLock": self._safe_diagnostics(),
        }
        self._log("error", "daemon.maintenance.stalled", **report)
        return report

    def status(self, now: float | None = None) -> dict[str, Any]:
        now = self._clock() if now is None else now
        with self._guard:
            stalled_since = self._stalled_since
            phase = self._phase if self._tick is not None else "between-ticks"
        if stalled_since is None:
            return {"stalled": False}
        return {
            "stalled": True,
            "phase": phase,
            "stalledSeconds": round(now - stalled_since, 1),
        }

    def _safe_diagnostics(self) -> Any:
        if self._diagnostics is None:
            return None
        try:
            return self._diagnostics()
        except Exception as error:  # noqa: BLE001 - a report must not fail
            return {"error": f"{type(error).__name__}: {error}"[:300]}

    def start(self, stop_event: threading.Event) -> None:
        if self._thread is not None:
            return

        # Paced by a private event, never by ``stop_event.wait``: the daemon's
        # stop event is shared, and callers (and tests) that time or patch its
        # waits -- the accept-loop backoff does -- must not see this poll.
        pace = threading.Event()

        def watch() -> None:
            while not stop_event.is_set():
                pace.wait(POLL_SECONDS)
                if stop_event.is_set():
                    return
                try:
                    self.check()
                except Exception:  # noqa: BLE001 - the watchdog outlives its own faults
                    pass

        self._thread = threading.Thread(
            target=watch, name="hyprial-maintenance-watchdog", daemon=True
        )
        self._thread.start()
