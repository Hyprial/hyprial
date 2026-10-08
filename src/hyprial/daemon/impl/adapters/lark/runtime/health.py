"""Independent liveness policy for the Lark inbound websocket.

The SDK process being alive and ordinary chat traffic being quiet are both
insufficient health signals.  This monitor combines business-event activity,
websocket control/data frames, and a credentialed REST endpoint probe.  All
clocks and side effects are injected so the production failure modes stay
deterministic in tests.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime


HealthReport = dict[str, object]


def report_reconcile_failure(
    sink: Callable[[dict[str, object]], None] | None,
    error: BaseException,
    **fields: object,
) -> None:
    """Name a sweep failure without carrying any of the exception's text.

    The platform/SDK message can hold URLs or access tokens -- the rule
    ``adapter.py`` states three times over -- so only the class name and the
    fields the call site knows locally leave this process.  Telemetry is
    best-effort: a failing sink must never change how a sweep fails.
    """

    if sink is None:
        return
    try:
        sink(
            {
                "event": "adapter.reconcile.sweep_failed",
                "status": "warning",
                "exceptionType": type(error).__name__,
                "node": "adapter-reconcile",
                **fields,
            }
        )
    except Exception:  # noqa: BLE001 - telemetry must not break a sweep
        pass


@dataclass(frozen=True)
class _PendingHealthReport:
    epoch: int
    sequence: int
    payload: HealthReport


class _HealthReportDispatcher:
    """Bounded, single-consumer telemetry delivery outside monitor locks."""

    def __init__(
        self,
        *,
        name: str,
        sink: Callable[[HealthReport], None],
        current: Callable[[_PendingHealthReport], bool],
        max_pending: int = 64,
        idle_timeout: float = 0.1,
    ) -> None:
        self._name = name
        self._sink = sink
        self._current = current
        self._max_pending = max_pending
        self._idle_timeout = idle_timeout
        self._condition = threading.Condition()
        self._pending: deque[_PendingHealthReport] = deque()
        self._active = False
        self._closed = False
        self._terminal_enqueued = False
        self._last_sequence = 0
        self._thread: threading.Thread | None = None

    def enqueue(self, report: _PendingHealthReport) -> bool:
        terminal = report.payload["streamHealth"] == "stale"
        with self._condition:
            if self._closed or (self._terminal_enqueued and not terminal):
                return False
            if terminal:
                if self._terminal_enqueued:
                    return False
                self._terminal_enqueued = True
                # No prepared non-terminal event may remain behind the fence.
                self._pending.clear()
            elif len(self._pending) >= self._max_pending:
                # Preserve a bounded prefix and coalesce the newest state.
                self._pending[-1] = report
                self._ensure_thread()
                self._condition.notify()
                return True
            self._pending.append(report)
            self._ensure_thread()
            self._condition.notify()
            return True

    def drain(self, timeout: float = 1.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: not self._pending and not self._active,
                timeout=timeout,
            )

    def close(self, timeout: float = 1.0) -> bool:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            thread = self._thread
        if thread is None or thread is threading.current_thread():
            return thread is None
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def pending_count(self) -> int:
        with self._condition:
            return len(self._pending)

    def thread_snapshot(self) -> Sequence[threading.Thread]:
        with self._condition:
            return () if self._thread is None else (self._thread,)

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"lark-health-report-{self._name}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            with self._condition:
                if not self._pending:
                    if self._closed:
                        self._thread = None
                        self._condition.notify_all()
                        return
                    self._condition.wait(timeout=self._idle_timeout)
                    if not self._pending:
                        self._thread = None
                        self._condition.notify_all()
                        return
                report = self._pending.popleft()
                self._active = True
            try:
                if (
                    report.sequence > self._last_sequence
                    and self._current(report)
                ):
                    self._last_sequence = report.sequence
                    self._sink(report.payload)
            except BaseException:
                # Telemetry failure must not kill health supervision or retain
                # potentially sensitive sink exceptions.
                pass
            finally:
                with self._condition:
                    self._active = False
                    self._condition.notify_all()


DEFAULT_STALE_AFTER_SECONDS = 15 * 60.0
DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS = 30.0
DEFAULT_REST_PROBE_TIMEOUT_SECONDS = 10.0
DEFAULT_IDLE_RECONCILE_TIMEOUT_SECONDS = 60.0
# Covers the worker's 1s stale observation window, daemon reconcile scheduling,
# a failed-spawn retry interval, SDK startup, and scheduling jitter.
RECOVERY_RESTART_MARGIN_SECONDS = 120.0


def required_reconcile_lookback(
    *,
    stale_after: float,
    health_interval: float,
    probe_timeout: float,
    reconcile_timeout: float = DEFAULT_IDLE_RECONCILE_TIMEOUT_SECONDS,
    restart_margin: float = RECOVERY_RESTART_MARGIN_SECONDS,
) -> int:
    """Smallest safe history window for the configured detection lifecycle."""

    values = (
        stale_after,
        health_interval,
        probe_timeout,
        reconcile_timeout,
        restart_margin,
    )
    if any(not math.isfinite(value) for value in values):
        raise ValueError("Lark recovery timing values must be finite")
    if any(value < 0 for value in values) or stale_after <= 0:
        raise ValueError("Lark recovery timing values must be non-negative")
    # Strictly exceed the full worst-case age, including fractional settings.
    return math.floor(sum(values)) + 1


DEFAULT_RECONCILE_LOOKBACK_SECONDS = 30 * 60
if DEFAULT_RECONCILE_LOOKBACK_SECONDS < required_reconcile_lookback(
    stale_after=DEFAULT_STALE_AFTER_SECONDS,
    health_interval=DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
    probe_timeout=DEFAULT_REST_PROBE_TIMEOUT_SECONDS,
    reconcile_timeout=DEFAULT_IDLE_RECONCILE_TIMEOUT_SECONDS,
):
    raise RuntimeError("default Lark reconcile window is shorter than recovery")


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class LarkStreamHealthMonitor:
    """Classify a connected Lark event stream without mistaking idle for dead."""

    def __init__(
        self,
        *,
        name: str,
        stale_after: float,
        rest_probe: Callable[[], None],
        reconcile_idle: Callable[[], int] = lambda: 0,
        probe_timeout: float = DEFAULT_REST_PROBE_TIMEOUT_SECONDS,
        reconcile_timeout: float = DEFAULT_IDLE_RECONCILE_TIMEOUT_SECONDS,
        probe_wait: Callable[[threading.Event, float], bool] | None = None,
        rebuild: Callable[[str], None],
        report: Callable[[HealthReport], None],
        monotonic: Callable[[], float],
        utcnow: Callable[[], datetime],
        events: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        if not math.isfinite(stale_after) or stale_after <= 0:
            raise ValueError("stale_after must be positive")
        if not math.isfinite(probe_timeout) or probe_timeout <= 0:
            raise ValueError("probe_timeout must be positive")
        if not math.isfinite(reconcile_timeout) or reconcile_timeout <= 0:
            raise ValueError("reconcile_timeout must be positive")
        self.name = name
        self.stale_after = stale_after
        self._rest_probe = rest_probe
        self._reconcile_idle = reconcile_idle
        self.probe_timeout = probe_timeout
        self.reconcile_timeout = reconcile_timeout
        self._probe_wait = probe_wait or (
            lambda completed, timeout: completed.wait(timeout)
        )
        self._rebuild = rebuild
        self._events = events
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._lock = threading.Lock()
        self._telemetry_epoch = 0
        self._next_report_sequence = 0
        self._connected_mono: float | None = None
        self._last_event_mono: float | None = None
        self._last_transport_mono: float | None = None
        self._last_probe_mono: float | None = None
        self._connected_at: datetime | None = None
        self._last_event_at: datetime | None = None
        self._last_transport_at: datetime | None = None
        self._last_probe_at: datetime | None = None
        self._rebuilding = False
        self._reconciling = False
        self._dispatcher = _HealthReportDispatcher(
            name=name,
            sink=report,
            current=self._report_is_current,
        )

    def connected(self) -> bool:
        now_mono, now_wall = self._monotonic(), self._utcnow()
        with self._lock:
            if self._rebuilding:
                return False
            self._connected_mono = now_mono
            self._last_transport_mono = now_mono
            self._connected_at = now_wall
            self._last_transport_at = now_wall
            self._last_probe_mono = None
            self._reconciling = False
            report = self._snapshot("healthy", "connected")
            return self._dispatcher.enqueue(report)

    def transport_activity(self) -> None:
        now_mono, now_wall = self._monotonic(), self._utcnow()
        with self._lock:
            self._last_transport_mono = now_mono
            self._last_transport_at = now_wall
            if self._rebuilding:
                return
            report = self._snapshot(
                "checking" if self._reconciling else "healthy",
                (
                    "history-reconcile-in-progress"
                    if self._reconciling
                    else "transport-frame"
                ),
            )
            self._dispatcher.enqueue(report)

    def event_activity(self) -> None:
        now_mono, now_wall = self._monotonic(), self._utcnow()
        with self._lock:
            self._last_event_mono = now_mono
            self._last_event_at = now_wall
            self._last_transport_mono = now_mono
            self._last_transport_at = now_wall
            if self._rebuilding:
                return
            report = self._snapshot(
                "checking" if self._reconciling else "healthy",
                (
                    "history-reconcile-in-progress"
                    if self._reconciling
                    else "business-event"
                ),
            )
            self._dispatcher.enqueue(report)

    def history_probe_failed(self) -> None:
        """Fail closed when an out-of-band reconnect sweep is incomplete."""

        self._mark_stale("history-probe-failed")

    def history_reconcile_started(self, *, coalesced: bool = False) -> bool:
        """Expose that history coverage is unresolved; never claim healthy."""

        with self._lock:
            if self._rebuilding:
                return False
            self._reconciling = True
            report = self._snapshot(
                "checking",
                (
                    "history-reconcile-coalesced"
                    if coalesced
                    else "history-reconcile-started"
                ),
            )
            return self._dispatcher.enqueue(report)

    def rebuild_latched(self) -> bool:
        """Whether this process is terminally committed to supervised exit."""

        with self._lock:
            return self._rebuilding

    def drain_telemetry(self, timeout: float = 1.0) -> bool:
        """Wait for queued telemetry; intended for orderly tests/shutdown."""

        return self._dispatcher.drain(timeout)

    def close(self, timeout: float = 1.0) -> bool:
        """Stop the best-effort daemon dispatcher after queued work drains."""

        return self._dispatcher.close(timeout)

    def check_once(self) -> None:
        """Probe an idle stream once and request at most one rebuild."""

        now = self._monotonic()
        with self._lock:
            if self._rebuilding or self._connected_mono is None:
                return
            if self._reconciling:
                report = self._snapshot(
                    "checking", "history-reconcile-in-progress"
                )
                probe = False
            else:
                event_baseline = max(
                    value
                    for value in (self._last_event_mono, self._connected_mono)
                    if value is not None
                )
                if now - event_baseline <= self.stale_after:
                    report = self._snapshot("healthy", "recent-business-event")
                    probe = False
                else:
                    transport_baseline = self._last_transport_mono
                    if transport_baseline is None:
                        transport_baseline = self._connected_mono
                    transport_stale = now - transport_baseline > self.stale_after
                    probe_recent = (
                        self._last_probe_mono is not None
                        and now - self._last_probe_mono <= self.stale_after
                    )
                    if probe_recent and not transport_stale:
                        report = self._snapshot(
                            "healthy", "business-idle-rest-healthy"
                        )
                        probe = False
                    else:
                        report = {}
                        probe = True
        if not probe:
            self._dispatcher.enqueue(report)
            return

        probe_failed, _unused = self._bounded_call(
            self._rest_probe, self.probe_timeout, "rest"
        )
        if probe_failed:
            self._mark_stale("rest-probe-failed")
            return
        reconcile_failed, missed_messages = self._bounded_call(
            self._reconcile_idle, self.reconcile_timeout, "history"
        )
        if reconcile_failed:
            self._mark_stale("history-probe-failed")
            return

        now = self._monotonic()
        with self._lock:
            self._last_probe_mono = now
            self._last_probe_at = self._utcnow()
            transport_baseline = self._last_transport_mono
            if transport_baseline is None:
                transport_baseline = self._connected_mono
            transport_stale = now - transport_baseline > self.stale_after
            if not transport_stale:
                report = self._snapshot(
                    "healthy", "business-idle-rest-healthy"
                )
        if missed_messages > 0:
            # Reconciliation has already redriven each unseen message through
            # the normal dedup/custody path. Rebuild the event subscription so
            # future messages return to the live websocket path.
            self._mark_stale("event-path-stale")
        elif transport_stale:
            self._mark_stale("websocket-transport-stale")
        else:
            self._dispatcher.enqueue(report)

    def _bounded_call(
        self, operation: Callable[[], object], timeout: float, label: str
    ) -> tuple[bool, int]:
        """Run one health operation under a hard deadline.

        Operations run sequentially and each owns at most one daemon thread. On
        timeout the rebuilding latch prevents another check, and production
        exits the worker, so a blocked SDK request cannot accumulate threads.
        """

        completed = threading.Event()
        result: dict[str, object] = {"failed": False, "value": 0}

        def probe() -> None:
            try:
                value = operation()
                result["value"] = max(0, int(value or 0))
            except BaseException as error:  # never retain or report credential-bearing detail
                result["failed"] = True
                # Only the class name goes out.  For label="history" this is
                # the constant RuntimeError raised by _reconcile_health_result,
                # and it never names the sweep's real cause; for label="rest"
                # it is the probe's own class.
                report_reconcile_failure(self._events, error, label=label)
            finally:
                completed.set()

        threading.Thread(
            target=probe,
            name=f"lark-{label}-probe-{self.name}",
            daemon=True,
        ).start()
        if not self._probe_wait(completed, timeout):
            return True, 0
        return bool(result["failed"]), int(result["value"])

    def _mark_stale(self, reason: str) -> None:
        with self._lock:
            if self._rebuilding:
                return
            self._rebuilding = True
            self._reconciling = False
            # Invalidate every non-terminal snapshot prepared before this
            # transition. The dispatcher re-checks this epoch immediately
            # before invoking the external sink.
            self._telemetry_epoch += 1
            # Record when the failed probe was attempted without retaining its
            # exception or any potentially sensitive request detail.
            self._last_probe_at = self._utcnow()
            report = self._snapshot("stale", reason)
            self._dispatcher.enqueue(report)
        # Supervised recovery is independent of best-effort telemetry.  A
        # permanently blocked sink can never postpone exit 75.
        self._rebuild(reason)

    def _report_is_current(self, report: _PendingHealthReport) -> bool:
        with self._lock:
            if report.epoch != self._telemetry_epoch:
                return False
            terminal = report.payload["streamHealth"] == "stale"
            return terminal == self._rebuilding

    def _snapshot(self, health: str, reason: str) -> _PendingHealthReport:
        self._next_report_sequence += 1
        return _PendingHealthReport(
            epoch=self._telemetry_epoch,
            sequence=self._next_report_sequence,
            payload={
                "status": "health",
                "name": self.name,
                "streamHealth": health,
                "reason": reason,
                "connectedAt": _timestamp(self._connected_at),
                "lastEventAt": _timestamp(self._last_event_at),
                "lastTransportAt": _timestamp(self._last_transport_at),
                "lastProbeAt": _timestamp(self._last_probe_at),
            },
        )
