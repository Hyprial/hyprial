from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
import pytest
import hyprial.daemon.impl.adapters.lark.worker.entry as lark_worker
from hyprial.daemon.impl.adapters.lark import ActorTarget, ReconcileReport
from hyprial.daemon.impl.adapters.lark.runtime.health import LarkStreamHealthMonitor, _PendingHealthReport
from hyprial.daemon.impl.adapters.lark.worker.entry import _rebuild_after_stale
from hyprial.daemon.impl.adapters.lark.worker.sweep import STALE_REBUILD_EXIT_CODE
from hyprial.kernel import Logger

from ..support import (
    _close_seam_health_monitors as _close_seam_health_monitors,
    _files_containing,
    _wait_for_reconcile_settlement,
    _worker_main_callback_seam,
)


def test_worker_main_reconnect_storm_hanging_history_is_single_flight_and_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blocked = threading.Event()

    def hanging(_call: int, _lookback: int) -> ReconcileReport:
        blocked.wait()
        return ReconcileReport(1, 0, 0, 0, 0)

    seam = _worker_main_callback_seam(monkeypatch, tmp_path, hanging)
    try:
        seam.stream.callbacks["on_ready"]()
        for _ in range(20):
            seam.stream.callbacks["on_reconnect"]()

        deadline = time.monotonic() + 1.0
        while not seam.rebuilds and time.monotonic() < deadline:
            time.sleep(0.005)
        assert seam.health.drain_telemetry()
        reconcile_threads = [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("lark-reconcile-lark-test")
        ]
        health = [
            value
            for line in capsys.readouterr().out.splitlines()
            if (value := json.loads(line)).get("status") == "health"
        ]

        assert seam.rebuilds == ["history-probe-failed"]
        assert len(reconcile_threads) <= 2
        assert seam.adapter.calls == 1
        assert health[-1]["streamHealth"] == "stale"
        assert all(item["streamHealth"] != "healthy" for item in health)
        assert any(
            item["reason"] == "history-reconcile-coalesced" for item in health
        )
    finally:
        blocked.set()


def test_worker_main_reconnect_storm_coalesces_one_final_required_sweep(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first_started = threading.Event()
    release_first = threading.Event()

    def controlled(call: int, _lookback: int) -> ReconcileReport:
        if call == 1:
            first_started.set()
            release_first.wait(timeout=2)
        return ReconcileReport(1, 0, 0, 0, 0)

    seam = _worker_main_callback_seam(monkeypatch, tmp_path, controlled)
    seam.stream.callbacks["on_ready"]()
    assert first_started.wait(timeout=1)
    for _ in range(100):
        seam.stream.callbacks["on_reconnect"]()
    release_first.set()

    deadline = time.monotonic() + 1.0
    while seam.adapter.calls < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    _wait_for_reconcile_settlement(seam.health)
    assert seam.health.drain_telemetry()
    health = [
        value
        for line in capsys.readouterr().out.splitlines()
        if (value := json.loads(line)).get("status") == "health"
    ]

    assert seam.adapter.calls == 2
    assert seam.rebuilds == []
    assert any(item["streamHealth"] == "checking" for item in health[:-1])
    assert health[-1]["streamHealth"] == "healthy"
    assert health[-1]["reason"] == "connected"


def test_coordinator_rejects_request_after_health_rebuild_is_latched() -> None:
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    calls: list[str] = []
    health = LarkStreamHealthMonitor(
        name="terminal-race",
        stale_after=900.0,
        rest_probe=lambda: None,
        rebuild=rebuilds.append,
        report=reports.append,
        monotonic=lambda: 0.0,
        utcnow=lambda: datetime(2026, 8, 10, tzinfo=UTC),
    )
    coordinator = lark_worker.ReconnectSweepCoordinator(
        name="terminal-race",
        reconcile=lambda: calls.append("history")
        or ReconcileReport(1, 0, 0, 0, 0),
        timeout=0.1,
        health=health,
    )
    health.connected()
    health.history_probe_failed()

    accepted = coordinator.request()
    health.transport_activity()
    health.event_activity()
    assert health.history_reconcile_started() is False
    assert health.drain_telemetry()

    assert accepted is False
    assert calls == []
    assert rebuilds == ["history-probe-failed"]
    stale_index = next(
        index
        for index, item in enumerate(reports)
        if item["streamHealth"] == "stale"
    )
    assert {
        item["streamHealth"] for item in reports[stale_index:]
    } == {"stale"}
    assert health.connected() is False


def test_coordinator_programming_error_is_visible_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    errors: list[BaseException] = []
    monkeypatch.setattr(
        threading,
        "excepthook",
        lambda args: errors.append(args.exc_value),
    )

    def broken_reconcile() -> ReconcileReport:
        raise NameError("missing reconcile symbol")

    health = LarkStreamHealthMonitor(
        name="programming-error",
        stale_after=900.0,
        rest_probe=lambda: None,
        rebuild=rebuilds.append,
        report=reports.append,
        monotonic=lambda: 0.0,
        utcnow=lambda: datetime(2026, 8, 10, tzinfo=UTC),
    )
    coordinator = lark_worker.ReconnectSweepCoordinator(
        name="programming-error",
        reconcile=broken_reconcile,
        timeout=0.1,
        health=health,
    )
    assert health.connected() is True
    assert coordinator.request() is True

    deadline = time.monotonic() + 1.0
    while (not errors or not rebuilds) and time.monotonic() < deadline:
        time.sleep(0.001)

    assert len(errors) == 1
    assert isinstance(errors[0], NameError)
    assert rebuilds == ["history-probe-failed"]
    assert health.rebuild_latched() is True
    assert reports[-1]["streamHealth"] == "stale"


def test_success_settlement_cannot_clear_concurrently_latched_rebuild() -> None:
    reports: list[dict[str, object]] = []
    rebuilds: list[str] = []
    started = threading.Event()
    release = threading.Event()

    def reconcile() -> ReconcileReport:
        started.set()
        release.wait(timeout=1)
        return ReconcileReport(1, 0, 0, 0, 0)

    health = LarkStreamHealthMonitor(
        name="settlement-race",
        stale_after=900.0,
        rest_probe=lambda: None,
        rebuild=rebuilds.append,
        report=reports.append,
        monotonic=lambda: 0.0,
        utcnow=lambda: datetime(2026, 8, 10, tzinfo=UTC),
    )
    coordinator = lark_worker.ReconnectSweepCoordinator(
        name="settlement-race",
        reconcile=reconcile,
        timeout=0.5,
        health=health,
    )
    assert health.connected() is True
    assert coordinator.request() is True
    assert started.wait(timeout=1)

    settlement_prepared = threading.Event()
    release_settlement = threading.Event()
    original_current = health._dispatcher._current

    def block_settlement(report: _PendingHealthReport) -> bool:
        if report.payload["reason"] == "connected":
            settlement_prepared.set()
            release_settlement.wait(timeout=1)
        return original_current(report)

    health._dispatcher._current = block_settlement
    try:
        release.set()
        assert settlement_prepared.wait(timeout=1)
        health.history_probe_failed()
        assert health.rebuild_latched() is True
    finally:
        release_settlement.set()
    assert health.drain_telemetry()

    assert rebuilds == ["history-probe-failed"]
    stale_index = next(
        index
        for index, item in enumerate(reports)
        if item["streamHealth"] == "stale"
    )
    assert {
        item["streamHealth"] for item in reports[stale_index:]
    } == {"stale"}


def test_blocked_health_sink_cannot_delay_exit_75_trigger() -> None:
    sink_entered = threading.Event()
    release_sink = threading.Event()
    pauses: list[float] = []
    exits: list[int] = []

    def blocked_sink(report: dict[str, object]) -> None:
        if report["reason"] == "connected":
            sink_entered.set()
            release_sink.wait(timeout=2)

    health = LarkStreamHealthMonitor(
        name="blocked-exit",
        stale_after=900.0,
        rest_probe=lambda: None,
        rebuild=lambda reason: _rebuild_after_stale(
            reason,
            pause=pauses.append,
            exit_process=exits.append,
        ),
        report=blocked_sink,
        monotonic=lambda: 0.0,
        utcnow=lambda: datetime(2026, 8, 10, tzinfo=UTC),
    )
    assert health.connected() is True
    assert sink_entered.wait(timeout=1)

    started = time.monotonic()
    health.history_probe_failed()
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert pauses == [1.0]
    assert exits == [STALE_REBUILD_EXIT_CODE]
    release_sink.set()
    assert health.drain_telemetry()


def test_worker_main_stale_latch_rejects_concurrent_successful_reconnects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def successful(_call: int, _lookback: int) -> ReconcileReport:
        return ReconcileReport(1, 0, 0, 0, 0)

    seam = _worker_main_callback_seam(monkeypatch, tmp_path, successful)
    seam.stream.callbacks["on_ready"]()
    deadline = time.monotonic() + 1.0
    while seam.adapter.calls < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    _wait_for_reconcile_settlement(seam.health)
    assert seam.health.drain_telemetry()
    capsys.readouterr()

    seam.health.history_probe_failed()
    for _round in range(10):
        workers = [
            threading.Thread(
                target=(
                    seam.health.history_probe_failed
                    if index % 2
                    else seam.stream.callbacks["on_reconnect"]
                )
            )
            for index in range(100)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=1)
    assert seam.health.drain_telemetry()
    health = [
        value
        for line in capsys.readouterr().out.splitlines()
        if (value := json.loads(line)).get("status") == "health"
    ]

    assert seam.adapter.calls == 1
    assert seam.rebuilds == ["history-probe-failed"]
    assert health[0]["streamHealth"] == "stale"
    assert {item["streamHealth"] for item in health} == {"stale"}


def test_worker_main_sweep_exception_names_its_class_and_never_its_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """⭐ The load-bearing emit: §3.0's ``adapter.reconcile.sweep_failed``.

    The two discard points spec §3.1 named never see this exception -- the
    wrapper registered as ``reconcile`` folds any ``Exception`` into ``None``
    first -- so this line is the only reason the sweep's cause is nameable.
    The class name must travel; the SDK text (which can carry URLs or
    tokens) must not, on the telemetry surface or on disk.
    """

    sentinel = "SDKSECRET-do-not-leak-9f3a"

    def raising(_call: int, _lookback: int):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError(f"getaddrinfo failed for {sentinel}")

    seam = _worker_main_callback_seam(monkeypatch, tmp_path, raising)
    seam.stream.callbacks["on_ready"]()
    deadline = time.monotonic() + 1.0
    while not seam.rebuilds and time.monotonic() < deadline:
        time.sleep(0.005)
    assert seam.health.drain_telemetry()
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    named = [
        item
        for item in lines
        if item.get("event") == "adapter.reconcile.sweep_failed"
    ]
    assert named, f"the cause must be named; saw {[i.get('event') for i in lines]}"
    assert named[0]["exceptionType"] == "OperationalError"
    assert named[0]["node"] == "adapter-reconcile"
    assert named[0]["status"] == "warning"
    # ⛔ The negative half, both surfaces: nothing carries the SDK text.
    assert all(sentinel not in json.dumps(item) for item in lines)
    assert _files_containing(tmp_path, sentinel) == []
    # Fail-closed is untouched: same rebuild, one sweep, no extra attempt.
    assert seam.rebuilds == ["history-probe-failed"]
    assert seam.adapter.calls == 1


def test_coordinator_names_a_base_exception_that_bypasses_the_sweep_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3.1 ``_call_once``: only non-``Exception`` BaseExceptions reach it."""

    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    reports: list[dict[str, object]] = []
    emitted: list[dict[str, object]] = []
    health = LarkStreamHealthMonitor(
        name="base-exception",
        stale_after=900.0,
        rest_probe=lambda: None,
        rebuild=lambda reason: None,
        report=reports.append,
        monotonic=lambda: 0.0,
        utcnow=lambda: datetime(2026, 8, 10, tzinfo=UTC),
    )

    def raising() -> ReconcileReport:
        raise KeyboardInterrupt("interrupted mid-call")

    coordinator = lark_worker.ReconnectSweepCoordinator(
        name="base-exception",
        reconcile=raising,
        timeout=0.5,
        health=health,
        report=emitted.append,
    )

    assert coordinator._call_once() == (True, None)
    assert emitted == [
        {
            "event": "adapter.reconcile.sweep_failed",
            "status": "warning",
            "exceptionType": "KeyboardInterrupt",
            "node": "adapter-reconcile",
            "stage": "sweep-call",
        }
    ]
    assert "interrupted mid-call" not in repr(emitted)


def test_end_to_end_phase_and_class_together_separate_the_two_cells(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """⭐ §3.2's criterion, both halves in one real composition.

    The marker alone gives the phase; ``adapter.reconcile.sweep_failed`` alone
    gives the class.  This drives the real ``reconcile_recent`` through the
    real worker closures (the seam) twice, once per cell, and reads both
    surfaces back:

      in-loop raise  -> ``sweep_started`` present  + exceptionType X
      pre-loop raise -> ``sweep_started`` absent   + exceptionType Y
    """

    from tests.daemon.adapters.lark.inbound.recovery.support import NOW_MS, inbound_message, make_adapter, sdk_event

    actor = ActorTarget(
        actor_id="agent:alice:codex:main",
        actor_key="alice/codex/main",
        display_name="alice/codex/main",
    )
    adapter_logger = Logger.adapter(tmp_path / "adapter-state", name="lark-test")
    adapter, lark, _harness, _routes = make_adapter(
        tmp_path, actor, logger=adapter_logger
    )

    def reconcile(_call: int, lookback: int):  # type: ignore[no-untyped-def]
        return adapter.reconcile_recent(
            lookback_seconds=lookback, now_ms=NOW_MS
        )

    def sweep_events() -> list[dict[str, object]]:
        if not adapter_logger.path.exists():
            return []
        return [
            json.loads(line)
            for line in adapter_logger.path.read_text().splitlines()
        ]

    def run_sweep() -> list[dict[str, object]]:
        seam = _worker_main_callback_seam(monkeypatch, tmp_path, reconcile)
        seam.stream.callbacks["on_ready"]()
        deadline = time.monotonic() + 1.0
        while not seam.rebuilds and time.monotonic() < deadline:
            time.sleep(0.005)
        assert seam.health.drain_telemetry()
        lines = [
            json.loads(line) for line in capsys.readouterr().out.splitlines()
        ]
        assert seam.rebuilds == ["history-probe-failed"]
        return lines

    def failure_classes(lines: list[dict[str, object]]) -> list[object]:
        return [
            item.get("exceptionType")
            for item in lines
            if item.get("event") == "adapter.reconcile.sweep_failed"
        ]

    # The monkeypatch below pins this adapter (and its sqlite fds) until after
    # the fd-hygiene check, so release it on every path (card #77).
    try:
        # Cell (ii): the loop is entered, then the body raises.
        sdk_event(adapter.handle_sdk_event, event_id="evt_1", message_id="om_one")
        lark.history["oc_dm"] = (inbound_message("om_missed"),)
        monkeypatch.setattr(
            "hyprial.daemon.impl.adapters.lark.inbound.reconcile._create_time_ms",
            lambda _value: (_ for _ in ()).throw(
                TypeError("synthetic in-loop failure")
            ),
        )
        in_loop_lines = run_sweep()

        assert failure_classes(in_loop_lines) == ["TypeError"]
        started = [
            item
            for item in sweep_events()
            if item.get("event") == "adapter.reconcile.sweep_started"
        ]
        assert [item["chatCount"] for item in started] == [1], (
            "in-loop: the marker must say the loop was reached"
        )

        # Cell (i): the scan set itself raises.
        monkeypatch.setattr(
            adapter._state,
            "recent_chats",
            lambda: (_ for _ in ()).throw(
                sqlite3.OperationalError("synthetic scan-set failure")
            ),
        )
        pre_loop_lines = run_sweep()

        assert failure_classes(pre_loop_lines) == ["OperationalError"]
        assert [
            item
            for item in sweep_events()
            if item.get("event") == "adapter.reconcile.sweep_started"
        ] == started, "pre-loop: the marker must still be absent"
    finally:
        adapter._state.close()
