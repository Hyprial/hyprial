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
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.actor_runtime.effects import EffectCompleted, EffectLane, EffectRequest

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


@dataclass(frozen=True, slots=True)
class _TickStarted:
    ident: int | None
    name: str
    at: float


@dataclass(frozen=True, slots=True)
class _Phase:
    name: str


@dataclass(frozen=True, slots=True)
class _TickFinished:
    at: float


@dataclass(frozen=True, slots=True)
class _Poll:
    at: float
    reply: "_PollReply | None" = None


@dataclass(slots=True)
class _PollReply:
    ready: threading.Event = field(default_factory=threading.Event)
    report: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _Barrier:
    ready: threading.Event


@dataclass(frozen=True, slots=True)
class _Notice:
    event: str
    phase: str
    elapsed_ms: int
    ident: int | None
    thread: str


class ActorMaintenanceWatchdog:
    """Mailbox-owned maintenance marks with bounded off-mailbox diagnostics."""

    def __init__(
        self,
        *,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
        log: Callable[..., None],
        diagnostics: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        runtime: ActorRuntime | None = None,
    ) -> None:
        self._stall_seconds = stall_seconds
        self._log = log
        self._diagnostics = diagnostics
        self._clock = clock
        self._runtime = runtime or ActorRuntime()
        self._projection_lock = threading.Lock()
        self._projection: dict[str, Any] = {"stalled": False}
        self._tick: tuple[int | None, str, float] | None = None
        self._phase = "start"
        self._last_finished: float | None = None
        self._reported_at: float | None = None
        self._stalled_since: float | None = None
        self._pending_recovery: _Notice | None = None
        self._closing = False
        self._closed = False
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None
        self._check_replies: dict[str, _PollReply] = {}
        self._handle = self._runtime.start(
            ActorSpec(
                name="maintenance-watchdog",
                handler_factory=lambda: self._receive,
                mailbox_capacity=256,
            )
        )
        try:
            self._effects: EffectLane[_Notice, dict[str, Any] | None] = EffectLane(
                name="maintenance-watchdog-report",
                execute=self._report,
                complete=lambda event: self._runtime.tell(self._handle, event),
                capacity=2,
            )
        except BaseException:
            # The application cannot close an object whose constructor did
            # not return. No command or effect has been admitted yet, so the
            # newly started actor must be fully stopped here.
            if not self._runtime.stop(self._handle, 5.0):
                raise RuntimeError(
                    "maintenance watchdog actor did not stop after effect construction failed"
                )
            raise

    def _submit(self, command: object) -> AdmissionResult:
        if self._closing:
            return AdmissionResult.CLOSED
        return self._runtime.tell(self._handle, command)

    def tick_started(self) -> None:
        current = threading.current_thread()
        self._submit(_TickStarted(current.ident, current.name, self._clock()))

    def phase(self, name: str) -> None:
        self._submit(_Phase(name))

    def tick_finished(self) -> None:
        self._submit(_TickFinished(self._clock()))

    def status(self, now: float | None = None) -> dict[str, Any]:
        with self._projection_lock:
            projection = dict(self._projection)
        if projection.get("stalled"):
            since = projection.pop("_since", None)
            if since is not None:
                projection["stalledSeconds"] = round(
                    (self._clock() if now is None else now) - since, 1
                )
        else:
            projection.pop("_since", None)
        return projection

    def check(self, now: float | None = None) -> dict[str, Any] | None:
        """Join the exact accepted report after diagnostics and logging finish."""
        reply = _PollReply()
        if (
            self._submit(_Poll(self._clock() if now is None else now, reply))
            is not AdmissionResult.ACCEPTED
        ):
            raise RuntimeError("maintenance watchdog is closing or overloaded")
        if not reply.ready.wait(2.0):
            # The effect keeps its accepted custody; a caller timeout does not
            # claim that no stall existed or cancel the log write.
            raise TimeoutError("maintenance watchdog report remains accepted")
        if reply.error is not None:
            raise RuntimeError(f"maintenance watchdog report failed: {reply.error}")
        return reply.report

    def start(self, stop_event: threading.Event) -> None:
        if self._poller is not None:
            return

        def watch() -> None:
            while not self._stop.wait(POLL_SECONDS):
                if stop_event.is_set():
                    return
                self._submit(_Poll(self._clock()))

        self._poller = threading.Thread(
            target=watch, name="hyprial-maintenance-watchdog", daemon=True
        )
        self._poller.start()

    def close(self, timeout: float = 5.0) -> bool:
        if self._closed:
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        self._closing = True
        self._stop.set()
        if self._poller is not None:
            self._poller.join(max(0.0, deadline - time.monotonic()))
            if self._poller.is_alive():
                return False
        barrier = threading.Event()
        if self._runtime.tell(
            self._handle, _Barrier(barrier)
        ) is not AdmissionResult.ACCEPTED or not barrier.wait(
            max(0.0, deadline - time.monotonic())
        ):
            return False
        # Do not close effect admission while an accepted log or deferred
        # recovery still owns custody. A bounded close can be retried after it
        # times out, and the effect lane remains available for the successor.
        while (
            self._effects.snapshot().outstanding or self._pending_recovery is not None
        ):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        if not self._closed:
            self._closed = self._runtime.stop(
                self._handle, max(0.0, deadline - time.monotonic())
            )
        return self._closed

    def _receive(self, command: object) -> None:
        if isinstance(command, _TickStarted):
            self._tick = (command.ident, command.name, command.at)
            self._phase = "start"
        elif isinstance(command, _Phase):
            self._phase = command.name
        elif isinstance(command, _TickFinished):
            tick = self._tick
            stalled = self._stalled_since is not None
            phase = self._phase
            self._tick = None
            self._last_finished = command.at
            self._stalled_since = None
            self._reported_at = None
            if stalled and tick is not None:
                notice = _Notice(
                    "daemon.maintenance.recovered",
                    phase,
                    int((command.at - tick[2]) * 1000),
                    None,
                    tick[1],
                )
                if self._send_notice(notice) is None:
                    self._pending_recovery = notice
        elif isinstance(command, _Poll):
            submitted = self._poll(command.at)
            if command.reply is not None and submitted is None:
                command.reply.ready.set()
            elif command.reply is not None:
                self._check_replies[submitted] = command.reply
        elif isinstance(command, EffectCompleted):
            self._effects.acknowledge(command.operation_id, command.generation)
            if (
                self._pending_recovery is not None
                and self._send_notice(self._pending_recovery) is not None
            ):
                self._pending_recovery = None
            reply = self._check_replies.pop(command.operation_id, None)
            if reply is not None:
                reply.report = command.result
                reply.error = command.error
                reply.ready.set()
        elif isinstance(command, _Barrier):
            command.ready.set()
        else:
            raise TypeError("unsupported maintenance watchdog command")
        self._publish_projection()

    def _poll(self, now: float) -> str | None:
        tick = self._tick
        if tick is not None:
            ident, name, since = tick
            phase = self._phase
        elif self._last_finished is not None:
            ident, name, since = None, "", self._last_finished
            phase = "between-ticks"
        else:
            return None
        elapsed = now - since
        if elapsed <= self._stall_seconds:
            return None
        if (
            self._reported_at is not None
            and now - self._reported_at < self._stall_seconds
        ):
            return None
        notice = _Notice(
            "daemon.maintenance.stalled", phase, int(elapsed * 1000), ident, name
        )
        operation_id = self._send_notice(notice)
        if operation_id is None:
            return None
        self._reported_at = now
        if self._stalled_since is None:
            self._stalled_since = since
        return operation_id

    def _send_notice(self, notice: _Notice) -> str | None:
        operation_id = uuid4().hex
        result = self._effects.submit(EffectRequest(operation_id, 1, notice))
        return operation_id if result is AdmissionResult.ACCEPTED else None

    def _publish_projection(self) -> None:
        if self._stalled_since is None:
            projection: dict[str, Any] = {"stalled": False}
        else:
            projection = {
                "stalled": True,
                "phase": self._phase if self._tick is not None else "between-ticks",
                "_since": self._stalled_since,
                "stalledSeconds": round(self._clock() - self._stalled_since, 1),
            }
        with self._projection_lock:
            self._projection = projection

    def _report(self, notice: _Notice) -> dict[str, Any] | None:
        if notice.event == "daemon.maintenance.recovered":
            self._log(
                "info", notice.event, phase=notice.phase, durationMs=notice.elapsed_ms
            )
            return None
        try:
            lock_holder = self._diagnostics() if self._diagnostics is not None else None
        except Exception as error:  # noqa: BLE001 - a report must not fail
            lock_holder = {"error": f"{type(error).__name__}: {error}"[:300]}
        report = {
            "phase": notice.phase,
            "elapsedMs": notice.elapsed_ms,
            "thread": notice.thread,
            "stack": _stack_of(notice.ident) if notice.ident is not None else "",
            "transportLock": lock_holder,
        }
        self._log("error", notice.event, **report)
        return report
