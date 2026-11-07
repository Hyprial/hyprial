"""Failure-shape tests for Lark inbound stream liveness.

Business-message silence is normal.  The health contract therefore combines
the last business event, websocket transport activity (including control
frames), and an active REST endpoint probe before declaring the stream stale.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from hyprial.daemon.impl.adapters.lark.worker.sweep import _reconcile_health_result
from hyprial.daemon.impl.adapters.lark.runtime.health import (
    DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
    DEFAULT_IDLE_RECONCILE_TIMEOUT_SECONDS,
    DEFAULT_RECONCILE_LOOKBACK_SECONDS,
    DEFAULT_REST_PROBE_TIMEOUT_SECONDS,
    DEFAULT_STALE_AFTER_SECONDS,
    LarkStreamHealthMonitor,
    _PendingHealthReport,
    required_reconcile_lookback,
)
class Clock:
    def __init__(self) -> None:
        self.monotonic = 0.0
        self.wall = datetime(2026, 8, 10, tzinfo=UTC)

    def advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.wall += timedelta(seconds=seconds)


def monitor(
    clock: Clock,
    *,
    probe: object,
    reports: list[dict[str, object]],
    rebuilds: list[str],
    reconcile_idle: object = lambda: 0,
    probe_wait: object | None = None,
    probe_timeout: float = 10.0,
    events: list[dict[str, object]] | None = None,
) -> LarkStreamHealthMonitor:
    kwargs = {}
    if probe_wait is not None:
        kwargs["probe_wait"] = probe_wait
    return LarkStreamHealthMonitor(  # type: ignore[arg-type]
        name="feishu-test",
        stale_after=900.0,
        rest_probe=probe,  # type: ignore[arg-type]
        reconcile_idle=reconcile_idle,  # type: ignore[arg-type]
        probe_timeout=probe_timeout,
        rebuild=lambda reason: rebuilds.append(reason),
        report=reports.append,
        monotonic=lambda: clock.monotonic,
        utcnow=lambda: clock.wall,
        events=None if events is None else events.append,
        **kwargs,
    )


def test_network_or_rest_probe_failure_marks_stale_and_rebuilds() -> None:
    clock = Clock()
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []

    def unavailable() -> None:
        raise OSError("synthetic network partition")

    health = monitor(clock, probe=unavailable, reports=reports, rebuilds=rebuilds)
    health.connected()
    clock.advance(901)

    health.check_once()

    assert health.drain_telemetry()
    assert rebuilds == ["rest-probe-failed"]
    assert reports[-1]["streamHealth"] == "stale"
    assert reports[-1]["reason"] == "rest-probe-failed"
    # Diagnostics must not leak exception text that can contain URLs or tokens.
    assert "synthetic" not in str(reports[-1])


def test_long_business_idle_with_healthy_transport_and_rest_stays_online() -> None:
    clock = Clock()
    probes: list[str] = []
    reconciles: list[str] = []
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []

    def healthy() -> None:
        probes.append("rest")

    health = monitor(
        clock,
        probe=healthy,
        reconcile_idle=lambda: reconciles.append("history") or 0,
        reports=reports,
        rebuilds=rebuilds,
    )
    health.connected()
    # No business event for longer than the threshold, but a websocket PONG or
    # other control frame proves that the subscribed transport is still alive.
    clock.advance(899)
    health.transport_activity()
    clock.advance(2)

    health.check_once()

    assert health.drain_telemetry()
    assert probes == ["rest"]
    assert reconciles == ["history"]
    assert rebuilds == []
    assert reports[-1]["streamHealth"] == "healthy"
    assert reports[-1]["reason"] == "business-idle-rest-healthy"
    assert reports[-1]["lastEventAt"] is None
    assert isinstance(reports[-1]["lastTransportAt"], str)


def test_hanging_rest_probe_hits_deadline_and_rebuilds_without_thread_growth() -> None:
    clock = Clock()
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    blocked = threading.Event()
    health = monitor(
        clock,
        probe=blocked.wait,
        reports=reports,
        rebuilds=rebuilds,
        probe_timeout=0.02,
    )
    health.connected()
    clock.advance(901)

    started = time.monotonic()
    health.check_once()
    elapsed = time.monotonic() - started
    threads = [
        thread
        for thread in threading.enumerate()
        if thread.name == "lark-rest-probe-feishu-test"
    ]
    health.check_once()  # rebuilding latch: no second hanging probe thread

    assert health.drain_telemetry()
    assert elapsed < 0.5
    assert len(threads) == 1
    assert len(
        [
            thread
            for thread in threading.enumerate()
            if thread.name == "lark-rest-probe-feishu-test"
        ]
    ) == 1
    assert rebuilds == ["rest-probe-failed"]
    assert reports[-1]["reason"] == "rest-probe-failed"
    blocked.set()  # let the one abandoned daemon call finish before later tests
    threads[0].join(timeout=0.2)


def test_hanging_history_probe_hits_its_own_deadline_without_thread_growth() -> None:
    clock = Clock()
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    blocked = threading.Event()
    health = LarkStreamHealthMonitor(
        name="feishu-test",
        stale_after=900.0,
        rest_probe=lambda: None,
        reconcile_idle=blocked.wait,
        probe_timeout=0.02,
        reconcile_timeout=0.02,
        rebuild=rebuilds.append,
        report=reports.append,
        monotonic=lambda: clock.monotonic,
        utcnow=lambda: clock.wall,
    )
    health.connected()
    clock.advance(901)

    started = time.monotonic()
    health.check_once()
    elapsed = time.monotonic() - started
    threads = [
        thread
        for thread in threading.enumerate()
        if thread.name == "lark-history-probe-feishu-test"
    ]
    health.check_once()

    assert health.drain_telemetry()
    assert elapsed < 0.5
    assert len(threads) == 1
    assert rebuilds == ["history-probe-failed"]
    assert reports[-1]["reason"] == "history-probe-failed"
    blocked.set()
    threads[0].join(timeout=0.2)


def test_pong_alive_but_missed_history_marks_event_path_stale() -> None:
    clock = Clock()
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    reconciles: list[str] = []

    def missed_history() -> int:
        reconciles.append("history")
        return 1

    health = monitor(
        clock,
        probe=lambda: None,
        reconcile_idle=missed_history,
        reports=reports,
        rebuilds=rebuilds,
    )
    health.connected()
    clock.advance(899)
    health.transport_activity()  # control/PONG path is still alive
    clock.advance(2)

    health.check_once()

    assert health.drain_telemetry()
    assert reconciles == ["history"]
    assert rebuilds == ["event-path-stale"]
    assert reports[-1]["streamHealth"] == "stale"
    assert reports[-1]["reason"] == "event-path-stale"


def test_reconnect_history_failure_uses_same_fail_closed_health_transition() -> None:
    clock = Clock()
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    health = monitor(
        clock,
        probe=lambda: None,
        reports=reports,
        rebuilds=rebuilds,
    )
    health.connected()

    health.history_probe_failed()

    assert health.drain_telemetry()
    assert rebuilds == ["history-probe-failed"]
    assert reports[-1]["streamHealth"] == "stale"
    assert reports[-1]["reason"] == "history-probe-failed"


def test_health_tick_cannot_overwrite_in_progress_reconcile_with_healthy() -> None:
    clock = Clock()
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    health = monitor(
        clock,
        probe=lambda: None,
        reports=reports,
        rebuilds=rebuilds,
    )
    health.connected()
    health.history_reconcile_started()

    health.check_once()
    health.transport_activity()
    health.event_activity()

    assert health.drain_telemetry()
    assert reports[-1]["streamHealth"] == "checking"
    assert reports[-1]["reason"] == "history-reconcile-in-progress"
    assert rebuilds == []


@pytest.mark.parametrize(
    ("method_name", "prepared_reason"),
    [
        ("connected", "connected"),
        ("transport_activity", "transport-frame"),
        ("event_activity", "business-event"),
        ("history_reconcile_started", "history-reconcile-started"),
    ],
)
def test_stale_fence_discards_prepared_nonterminal_snapshot(
    method_name: str,
    prepared_reason: str,
) -> None:
    clock = Clock()
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    health = monitor(
        clock,
        probe=lambda: None,
        reports=reports,
        rebuilds=rebuilds,
    )
    assert health.connected() is True
    assert health.drain_telemetry()
    reports.clear()

    prepared = threading.Event()
    release_prepared = threading.Event()
    original_current = health._dispatcher._current

    def block_before_send(report: _PendingHealthReport) -> bool:
        if report.payload["reason"] == prepared_reason:
            prepared.set()
            release_prepared.wait(timeout=1)
        return original_current(report)

    health._dispatcher._current = block_before_send
    try:
        getattr(health, method_name)()
        assert prepared.wait(timeout=1)
        health.history_probe_failed()
        assert health.rebuild_latched() is True
        assert rebuilds == ["history-probe-failed"]
        assert reports == []
    finally:
        release_prepared.set()

    assert health.drain_telemetry()
    assert [report["streamHealth"] for report in reports] == ["stale"]


def test_blocked_external_report_cannot_delay_terminal_state_or_publish_late() -> None:
    clock = Clock()
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    old_sink_entered = threading.Event()
    release_old_sink = threading.Event()

    def blocked_report(report: dict[str, object]) -> None:
        if report["reason"] == "transport-frame":
            old_sink_entered.set()
            release_old_sink.wait(timeout=1)
        reports.append(report)

    health = LarkStreamHealthMonitor(
        name="feishu-test",
        stale_after=900.0,
        rest_probe=lambda: None,
        rebuild=rebuilds.append,
        report=blocked_report,
        monotonic=lambda: clock.monotonic,
        utcnow=lambda: clock.wall,
    )
    assert health.connected() is True
    health.transport_activity()
    assert old_sink_entered.wait(timeout=1)

    started = time.monotonic()
    health.history_probe_failed()
    elapsed = time.monotonic() - started
    # The external sink is blocked, but terminal recovery never waits for it.
    assert elapsed < 0.1
    assert health.rebuild_latched() is True
    assert rebuilds == ["history-probe-failed"]
    release_old_sink.set()
    assert health.drain_telemetry()

    stale_index = next(
        index
        for index, report in enumerate(reports)
        if report["streamHealth"] == "stale"
    )
    assert reports[-1]["streamHealth"] == "stale"
    assert {
        report["streamHealth"] for report in reports[stale_index:]
    } == {"stale"}


@pytest.mark.parametrize(
    ("method_name", "reentrant_reason", "initial_health"),
    [
        ("connected", "connected", "healthy"),
        ("transport_activity", "transport-frame", "healthy"),
        ("event_activity", "business-event", "healthy"),
        (
            "history_reconcile_started",
            "history-reconcile-started",
            "checking",
        ),
    ],
)
def test_reentrant_sink_enqueues_stale_after_current_callback(
    method_name: str,
    reentrant_reason: str,
    initial_health: str,
) -> None:
    clock = Clock()
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    holder: list[LarkStreamHealthMonitor] = []
    armed = [False]

    def reentrant_report(report: dict[str, object]) -> None:
        if armed[0] and report["reason"] == reentrant_reason:
            holder[0].history_probe_failed()
        reports.append(report)

    health = LarkStreamHealthMonitor(
        name="reentrant",
        stale_after=900.0,
        rest_probe=lambda: None,
        rebuild=rebuilds.append,
        report=reentrant_report,
        monotonic=lambda: clock.monotonic,
        utcnow=lambda: clock.wall,
    )
    holder.append(health)
    assert health.connected() is True
    assert health.drain_telemetry()
    reports.clear()

    armed[0] = True
    getattr(health, method_name)()
    assert health.drain_telemetry()
    assert rebuilds == ["history-probe-failed"]
    assert [report["streamHealth"] for report in reports] == [
        initial_health,
        "stale",
    ]


def test_slow_sink_storm_is_bounded_and_terminal_coalesces_queue() -> None:
    clock = Clock()
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    sink_entered = threading.Event()
    release_sink = threading.Event()

    def slow_report(report: dict[str, object]) -> None:
        if report["reason"] == "connected":
            sink_entered.set()
            release_sink.wait(timeout=2)
        reports.append(report)

    health = LarkStreamHealthMonitor(
        name="bounded",
        stale_after=900.0,
        rest_probe=lambda: None,
        rebuild=rebuilds.append,
        report=slow_report,
        monotonic=lambda: clock.monotonic,
        utcnow=lambda: clock.wall,
    )
    assert health.connected() is True
    assert sink_entered.wait(timeout=1)

    for _ in range(1_000):
        health.transport_activity()
        health.event_activity()
        health.history_reconcile_started(coalesced=True)
    assert health._dispatcher.pending_count() <= 64

    health.history_probe_failed()
    assert health._dispatcher.pending_count() == 1
    assert rebuilds == ["history-probe-failed"]
    release_sink.set()
    assert health.drain_telemetry()

    assert reports[-1]["streamHealth"] == "stale"
    assert len(reports) == 2


def test_dispatcher_is_daemon_and_close_cleans_idle_thread() -> None:
    clock = Clock()
    health = monitor(
        clock,
        probe=lambda: None,
        reports=[],
        rebuilds=[],
    )
    assert health.connected() is True
    assert health.drain_telemetry()
    threads = health._dispatcher.thread_snapshot()
    assert len(threads) == 1
    assert threads[0].daemon is True

    assert health.close(timeout=1) is True
    assert health._dispatcher.thread_snapshot() == ()


def test_connected_socket_that_stops_receiving_frames_is_rebuilt() -> None:
    clock = Clock()
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    health = monitor(
        clock,
        probe=lambda: None,
        reports=reports,
        rebuilds=rebuilds,
    )
    health.connected()
    # The process and SDK client stay alive, but no event *or control frame*
    # arrives. A successful REST probe isolates the failure to the websocket.
    clock.advance(901)

    health.check_once()

    assert health.drain_telemetry()
    assert rebuilds == ["websocket-transport-stale"]
    assert reports[-1]["streamHealth"] == "stale"
    assert reports[-1]["reason"] == "websocket-transport-stale"
    assert isinstance(reports[-1]["lastProbeAt"], str)


def test_recent_business_event_does_not_probe_or_rebuild() -> None:
    clock = Clock()
    probes: list[str] = []
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    health = monitor(
        clock,
        probe=lambda: probes.append("rest"),
        reports=reports,
        rebuilds=rebuilds,
    )
    health.connected()
    clock.advance(899)
    health.event_activity()
    clock.advance(2)

    health.check_once()

    assert health.drain_telemetry()
    assert probes == []
    assert rebuilds == []
    assert reports[-1]["streamHealth"] == "healthy"
    assert isinstance(reports[-1]["lastEventAt"], str)


def test_default_reconcile_window_strictly_covers_detection_and_restart() -> None:
    minimum = required_reconcile_lookback(
        stale_after=DEFAULT_STALE_AFTER_SECONDS,
        health_interval=DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
        probe_timeout=DEFAULT_REST_PROBE_TIMEOUT_SECONDS,
        reconcile_timeout=DEFAULT_IDLE_RECONCILE_TIMEOUT_SECONDS,
    )

    assert DEFAULT_RECONCILE_LOOKBACK_SECONDS >= minimum
    assert DEFAULT_RECONCILE_LOOKBACK_SECONDS > (
        DEFAULT_STALE_AFTER_SECONDS
        + DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
        + DEFAULT_REST_PROBE_TIMEOUT_SECONDS
        + DEFAULT_IDLE_RECONCILE_TIMEOUT_SECONDS
    )


def test_required_reconcile_lookback_rejects_non_finite_timing() -> None:
    for value in (math.inf, -math.inf, math.nan):
        try:
            required_reconcile_lookback(
                stale_after=value,
                health_interval=30.0,
                probe_timeout=10.0,
            )
        except ValueError:
            continue
        raise AssertionError(f"non-finite recovery timing accepted: {value!r}")


def test_a_rest_probe_failure_names_its_class_and_its_label() -> None:
    """§3.1 ``_bounded_call``, label="rest": this is the half that works.

    The class name is the probe's own, and ``label`` says which probe, so a
    reader can tell a transport failure from a history failure without any
    of the exception's text.
    """

    clock = Clock()
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    events: list[dict[str, object]] = []

    def unavailable() -> None:
        raise OSError("synthetic network partition")

    health = monitor(
        clock,
        probe=unavailable,
        reports=reports,
        rebuilds=rebuilds,
        events=events,
    )
    health.connected()
    clock.advance(901)

    health.check_once()

    assert health.drain_telemetry()
    assert rebuilds == ["rest-probe-failed"]
    assert events == [
        {
            "event": "adapter.reconcile.sweep_failed",
            "status": "warning",
            "exceptionType": "OSError",
            "node": "adapter-reconcile",
            "label": "rest",
        }
    ]
    # ⛔ Same rule as the adapter's own emits: the text does not travel.
    assert "synthetic" not in repr(events)


def test_a_history_probe_failure_reports_the_constant_runtime_error() -> None:
    """§3.1 ``_bounded_call``, label="history": a constant, not a cause.

    The operation handed to ``_bounded_call`` is worker.main's
    ``reconcile_missed``, which folds any sweep exception into ``None`` and
    then raises the fixed ``RuntimeError("Lark history reconciliation
    incomplete")``.  So this emit can never name the sweep's real cause; it
    is a probe-failure marker.  The real class comes from
    ``adapter.reconcile.sweep_failed`` instead (see the worker tests).
    """

    clock = Clock()
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    events: list[dict[str, object]] = []

    def reconcile_missed_like() -> int:
        # The production composition, verbatim in shape: swallow, then fail
        # closed on the missing report.
        try:
            raise sqlite3.OperationalError("synthetic sweep failure")
        except Exception:  # noqa: BLE001 - mirrors worker.main's wrapper
            report = None
        return _reconcile_health_result(report)

    health = monitor(
        clock,
        probe=lambda: None,
        reconcile_idle=reconcile_missed_like,
        reports=reports,
        rebuilds=rebuilds,
        events=events,
    )
    health.connected()
    clock.advance(901)

    health.check_once()

    assert health.drain_telemetry()
    assert rebuilds == ["history-probe-failed"]
    assert [(item["label"], item["exceptionType"]) for item in events] == [
        ("history", "RuntimeError")
    ]
    assert "synthetic" not in repr(events)
