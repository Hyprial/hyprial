"""Isolated actor counterpart to the #891 maintenance diagnostic assertions.

The module is loaded directly so these controls can run while unrelated
daemon imports in a dev-integration worktree still contain conflict markers.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def watchdog_module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3] / 'src/hyprial/daemon/impl/lifecycle/maintenance_watchdog.py'
    )
    spec = importlib.util.spec_from_file_location("watchdog_actor_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _Log:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, level: str, event: str, **fields: Any) -> None:
        self.records.append((level, event, fields))

    def events(self, name: str) -> list[dict[str, Any]]:
        return [fields for _, event, fields in self.records if event == name]


def test_actor_hung_phase_returns_full_report_after_logging(
    watchdog_module: ModuleType,
) -> None:
    clock, log = _Clock(), _Log()
    holder = {
        "thread": "hyprial-daemon-ipc-client",
        "role": "lock",
        "heldSeconds": 42.0,
        "stack": "...",
    }
    watchdog = watchdog_module.ActorMaintenanceWatchdog(
        stall_seconds=60, log=log, diagnostics=lambda: holder, clock=clock
    )
    entered, release = threading.Event(), threading.Event()

    def blocked_forwarding_tick() -> None:
        watchdog.tick_started()
        watchdog.phase("forwarding.timer")
        entered.set()
        release.wait(5)
        watchdog.tick_finished()

    tick = threading.Thread(
        target=blocked_forwarding_tick,
        name="daemon-maintenance-test",
        daemon=True,
    )
    tick.start()
    try:
        assert entered.wait(1)
        clock.now += 59
        assert watchdog.check() is None, "reported before the stall budget"
        clock.now += 2
        report = watchdog.check()
        assert report is not None
        assert report["phase"] == "forwarding.timer"
        assert report["thread"] == "daemon-maintenance-test"
        assert "blocked_forwarding_tick" in report["stack"]
        assert report["transportLock"] == holder
        stalled = log.events("daemon.maintenance.stalled")
        assert len(stalled) == 1
        assert stalled[0] == report
        assert stalled[0]["elapsedMs"] >= 61_000

        clock.now += 10
        assert watchdog.check() is None
        assert len(log.events("daemon.maintenance.stalled")) == 1
        clock.now += 60
        assert watchdog.check() is not None
        assert len(log.events("daemon.maintenance.stalled")) == 2
        status = watchdog.status()
        assert status["stalled"] is True
        assert status["phase"] == "forwarding.timer"
    finally:
        release.set()
        tick.join(5)
        assert watchdog.close(2.0)
    assert len(log.events("daemon.maintenance.recovered")) == 1
    assert watchdog.status()["stalled"] is False


def test_actor_scheduler_gap_and_original_thresholds(
    watchdog_module: ModuleType,
) -> None:
    assert watchdog_module.DEFAULT_STALL_SECONDS == 60.0
    assert watchdog_module.POLL_SECONDS == 5.0
    clock, log = _Clock(), _Log()
    watchdog = watchdog_module.ActorMaintenanceWatchdog(
        stall_seconds=60, log=log, diagnostics=lambda: None, clock=clock
    )
    try:
        assert watchdog.check() is None, "startup before any tick is not a stall"
        watchdog.tick_started()
        watchdog.tick_finished()
        clock.now += 60
        assert watchdog.check() is None
        clock.now += 1
        report = watchdog.check()
        assert report is not None
        assert report["phase"] == "between-ticks"
        assert report["stack"] == ""
        assert report["transportLock"] is None
        assert log.events("daemon.maintenance.stalled") == [report]
    finally:
        assert watchdog.close(2.0)


def test_actor_check_joins_exact_blocked_report_and_close_keeps_custody(
    watchdog_module: ModuleType,
) -> None:
    clock = _Clock()
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    records: list[dict[str, Any]] = []

    def log(_level: str, event: str, **fields: Any) -> None:
        if event == "daemon.maintenance.stalled":
            entered.set()
            assert release.wait(2.0)
            records.append(fields)

    watchdog = watchdog_module.ActorMaintenanceWatchdog(
        stall_seconds=60, log=log, diagnostics=lambda: {"role": "lock"}, clock=clock
    )
    watchdog.tick_started()
    watchdog.phase("runtime.timer")
    clock.now += 61
    outcome: list[dict[str, Any] | None] = []

    def check() -> None:
        outcome.append(watchdog.check())
        returned.set()

    caller = threading.Thread(target=check, daemon=True)
    caller.start()
    try:
        assert entered.wait(1.0)
        assert not returned.wait(0.02), "check returned before its log settled"
        started = time.monotonic()
        watchdog.phase("still-running")
        assert time.monotonic() - started < 0.1
        assert not watchdog.close(0.02), "accepted report lost close custody"
    finally:
        release.set()
        caller.join(2.0)
        assert watchdog.close(2.0)
    assert returned.is_set()
    assert len(outcome) == 1 and outcome[0] == records[0]
    assert outcome[0]["phase"] == "runtime.timer"
    assert outcome[0]["transportLock"] == {"role": "lock"}


@pytest.fixture(params=[False, True])
def sibling_watchdog(watchdog_module: ModuleType, request):
    sibling = (
        watchdog_module.ActorMaintenanceWatchdog(log=lambda *_a, **_k: None)
        if request.param else None
    )
    try:
        yield sibling
    finally:
        if sibling is not None:
            assert sibling.close(2.0)


def test_effect_lane_constructor_failure_stops_the_unreturned_actor(
    watchdog_module: ModuleType, monkeypatch: pytest.MonkeyPatch, sibling_watchdog,
) -> None:
    threads_before = frozenset(threading.enumerate())
    runtime = watchdog_module.ActorRuntime(event_sink=lambda _event: None)
    handles = []
    stopped = []
    real_start = runtime.start
    real_stop = runtime.stop

    def start(spec):  # type: ignore[no-untyped-def]
        handle = real_start(spec)
        handles.append(handle)
        return handle

    def stop(handle, timeout):  # type: ignore[no-untyped-def]
        result = real_stop(handle, timeout)
        stopped.append(result)
        return result

    def fail_effect_lane(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("injected second allocation failure")

    monkeypatch.setattr(runtime, "start", start)
    monkeypatch.setattr(runtime, "stop", stop)
    monkeypatch.setattr(watchdog_module, "EffectLane", fail_effect_lane)

    with pytest.raises(RuntimeError, match="injected second allocation failure"):
        watchdog_module.ActorMaintenanceWatchdog(
            log=lambda *_a, **_k: None, runtime=runtime
        )

    assert len(handles) == 1
    assert stopped == [True]
    snapshot = runtime.snapshot(handles[0])
    assert snapshot.state.value == "stopped"
    assert snapshot.queued == 0 and snapshot.in_flight == 0
    assert runtime.tell(handles[0], object()) is watchdog_module.AdmissionResult.CLOSED
    assert not any(
        thread not in threads_before
        and thread.name.startswith("maintenance-watchdog-report") and thread.is_alive()
        for thread in threading.enumerate()
    )
    if sibling_watchdog is not None:
        assert any(
            thread in threads_before
            and thread.name.startswith("maintenance-watchdog-report") and thread.is_alive()
            for thread in threading.enumerate()
        )
