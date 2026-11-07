"""The state writer alarms when it stays saturated (guard 3, agreed with Allen).

Production 0.5.0, 2026-10-05: the single state writer executed ~0.5 s per
write and was busy essentially all the time for hours, with nothing reporting
it.  Review 821 on #1145 showed that a per-tick average-latency rule cannot
fire on that shape (a 1 s tick completes ~2 such commands; fast reads dilute
the average; callers time out so the mailbox stays shallow).  The alarm now
judges utilization -- execution time over elapsed time -- across a window
built up over many ticks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.state_persistence.alarm import StateWriterAlarm

SLOW_KEY = "RecordSessionAgentEffects/ipc"
FAST_KEY = "ReadDesiredQuery/background"


class _Writer:
    """Cumulative execution counters as the authority reports them."""

    def __init__(self) -> None:
        self.rows: dict[str, list[float]] = {}
        self.queued = 0

    def run(self, key: str, count: int, each_ms: float) -> None:
        row = self.rows.setdefault(key, [0, 0.0])
        row[0] += count
        row[1] += count * each_ms

    def status(self) -> dict[str, Any]:
        return {
            "mailbox": {"queued": self.queued, "inFlight": 1, "capacity": 128, "pendingCompletions": 0},
            "execution": {k: {"calls": int(c), "errors": 0, "wallMs": w} for k, (c, w) in self.rows.items()},
            "roundTrip": {},
        }


def _ticks(alarm: StateWriterAlarm, writer: _Writer, now: list[float], seconds: int, work) -> list[tuple[str, dict]]:  # type: ignore[no-untyped-def]
    outcomes = []
    for _ in range(seconds):
        now[0] += 1.0
        work(writer)
        outcome = alarm.observe(writer.status())
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def _saturated(writer: _Writer) -> None:
    # The incident's shape: ~2 commands at ~480 ms each per 1 s tick, plus
    # many fast reads that would dilute any per-command average.
    writer.run(SLOW_KEY, 2, 480.0)
    writer.run(FAST_KEY, 40, 0.5)


def test_a_writer_busy_all_the_time_alarms_once_the_window_fills() -> None:
    now = [0.0]
    alarm = StateWriterAlarm(clock=lambda: now[0])
    writer = _Writer()
    outcomes = _ticks(alarm, writer, now, 40, _saturated)
    assert [kind for kind, _ in outcomes] == ["slow"]
    fields = outcomes[0][1]
    assert fields["utilization"] >= 0.95
    assert fields["windowMs"] >= 30_000
    assert fields["busiestCommand"] == SLOW_KEY


def test_a_healthy_writer_and_a_short_burst_stay_quiet() -> None:
    now = [0.0]
    alarm = StateWriterAlarm(clock=lambda: now[0])
    writer = _Writer()
    assert _ticks(alarm, writer, now, 40, lambda w: w.run(FAST_KEY, 20, 2.0)) == []
    # 5 s saturated inside a 30 s window is ~17 %: not an incident.
    assert _ticks(alarm, writer, now, 5, _saturated) == []
    assert _ticks(alarm, writer, now, 25, lambda w: w.run(FAST_KEY, 20, 2.0)) == []


def test_a_backed_up_mailbox_alarms_at_once() -> None:
    now = [0.0]
    alarm = StateWriterAlarm(clock=lambda: now[0])
    writer = _Writer()
    writer.queued = 40
    outcomes = _ticks(alarm, writer, now, 1, lambda w: None)
    assert outcomes and outcomes[0][0] == "slow" and outcomes[0][1]["queued"] == 40


def test_alarms_repeat_only_after_the_cooldown_and_recovery_is_reported() -> None:
    now = [0.0]
    alarm = StateWriterAlarm(clock=lambda: now[0], cooldown_seconds=300.0)
    writer = _Writer()
    first = _ticks(alarm, writer, now, 100, _saturated)
    assert [kind for kind, _ in first] == ["slow"]  # not one per tick
    later = _ticks(alarm, writer, now, 300, _saturated)
    assert [kind for kind, _ in later] == ["slow"]  # once more after 5 min
    calm = _ticks(alarm, writer, now, 60, lambda w: w.run(FAST_KEY, 10, 1.0))
    assert [kind for kind, _ in calm] == ["recovered"]


def test_renewed_saturation_logs_without_repaging_within_the_cooldown() -> None:
    # Review 822: clearing the cooldown on recovery paged ~40/h on a square wave.
    now = [0.0]
    alarm = StateWriterAlarm(clock=lambda: now[0], cooldown_seconds=300.0)
    writer = _Writer()
    calm = lambda w: w.run(FAST_KEY, 10, 1.0)  # noqa: E731
    outcomes: list[tuple[str, dict]] = []
    for _ in range(4):  # ~4 swings inside 300 s
        outcomes += _ticks(alarm, writer, now, 40, _saturated)
        outcomes += _ticks(alarm, writer, now, 35, calm)
    assert [kind for kind, _ in outcomes] == [
        "slow", "recovered",
        "slow", "recovered",
        "slow", "recovered",
        "slow", "recovered",
    ]
    assert [fields.get("_page", True) for kind, fields in outcomes if kind == "slow"] == [
        True,
        False,
        False,
        False,
    ]


def test_a_queue_hovering_at_the_limit_pages_at_most_once_per_cooldown() -> None:
    now = [0.0]
    alarm = StateWriterAlarm(clock=lambda: now[0], cooldown_seconds=300.0)
    writer = _Writer()
    slow = 0
    for second in range(900):
        now[0] += 1.0
        writer.queued = 33 if second % 2 else 31
        outcome = alarm.observe(writer.status())
        slow += outcome is not None and outcome[0] == "slow"
    assert slow == 3


def test_recovery_waits_for_the_mailbox_to_drain() -> None:
    now = [0.0]
    alarm = StateWriterAlarm(clock=lambda: now[0])
    writer = _Writer()
    writer.queued = 40
    assert [k for k, _ in _ticks(alarm, writer, now, 1, lambda w: None)] == ["slow"]
    writer.queued = 20  # idle writer, but still half the limit queued
    assert _ticks(alarm, writer, now, 40, lambda w: None) == []
    writer.queued = 0
    assert [k for k, _ in _ticks(alarm, writer, now, 1, lambda w: None)] == ["recovered"]


def test_a_failing_alarm_check_and_log_still_reschedule_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review 822: the reschedule sat after an unguarded log in the finally.
    from tests.daemon.ipc.cost.test_ipc_cost_counters import _app_injections

    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="op",
        **_app_injections(),
    )
    scheduled: list[float] = []

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected")

    monkeypatch.setattr(app, "_check_state_writer_latency", boom)
    monkeypatch.setattr(app, "_log_trace", boom)
    monkeypatch.setattr(
        app, "_schedule_maintenance", lambda generation, delay: scheduled.append(delay)
    )
    try:
        with pytest.raises(RuntimeError):
            app._scheduled_maintenance(app._maintenance_generation)
        assert scheduled == [1.0]
    finally:
        monkeypatch.undo()  # _close logs through _log_trace too
        app._close()
        app._close_home_lease()


def test_tick_finished_failure_still_reschedules_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.daemon.ipc.cost.test_ipc_cost_counters import _app_injections

    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="op",
        **_app_injections(),
    )
    scheduled: list[float] = []

    def boom() -> None:
        raise RuntimeError("injected tick-finished failure")

    monkeypatch.setattr(app._maintenance_watchdog, "tick_finished", boom)
    monkeypatch.setattr(app, "_schedule_maintenance", lambda generation, delay: scheduled.append(delay))
    try:
        with pytest.raises(RuntimeError, match="tick-finished"):
            app._scheduled_maintenance(app._maintenance_generation)
        assert scheduled == [1.0]
    finally:
        monkeypatch.undo()
        app._close()
        app._close_home_lease()


def test_a_failed_tick_still_checks_the_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Review 821: deleting the call left every test green; and a tick that
    # raised skipped the check.
    from tests.daemon.ipc.cost.test_ipc_cost_counters import _app_injections

    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="op",
        **_app_injections(),
    )
    checks: list[int] = []
    monkeypatch.setattr(app, "_check_state_writer_latency", lambda: checks.append(1))
    monkeypatch.setattr(app, "_schedule_maintenance", lambda generation, delay: None)

    def failing_domains(started):  # type: ignore[no-untyped-def]
        raise RuntimeError("injected tick failure")

    monkeypatch.setattr(app, "_run_scheduled_domains", failing_domains)
    try:
        app._scheduled_maintenance(app._maintenance_generation)
        assert checks == [1]
    finally:
        app._close()
        app._close_home_lease()


def test_the_real_writer_saturated_by_slow_writes_alarms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The reviewer's reproduction, shortened: a real authority whose writes
    # take 0.5 s, kept busy by two callers, observed once a second.  The old
    # per-tick rule stayed silent here for 20 s.
    import threading
    import time as real_time

    from hyprial.daemon.impl.desired_state import DesiredStateStore
    from hyprial.daemon.impl.state_db import StateDatabase
    from hyprial.daemon.impl.state_persistence import StatePersistenceAuthority

    state_db = StateDatabase(tmp_path / "lifecycle-operations.sqlite3")
    authority = StatePersistenceAuthority(DesiredStateStore(tmp_path / "desired.json", state_db))
    original = authority._journal_store.next_generation

    def slow_write():  # type: ignore[no-untyped-def]
        real_time.sleep(0.5)
        return original()

    monkeypatch.setattr(authority._journal_store, "next_generation", slow_write)
    stop = threading.Event()

    def caller() -> None:
        while not stop.is_set():
            authority.journal.next_generation()

    callers = [threading.Thread(target=caller, daemon=True) for _ in range(2)]
    alarm = StateWriterAlarm(window_seconds=3.0)
    outcomes = []
    try:
        for thread in callers:
            thread.start()
        for _ in range(6):
            real_time.sleep(1.0)
            outcome = alarm.observe(authority.latency_status())
            if outcome is not None:
                outcomes.append(outcome)
    finally:
        stop.set()
        for thread in callers:
            thread.join(5)
        assert authority.close()
    assert [kind for kind, _ in outcomes] == ["slow"], outcomes
    assert outcomes[0][1]["utilization"] >= 0.8


class _Daemon:
    from hyprial.daemon.impl.application.messaging.maintenance import _MaintenanceMixin as _M

    _check_state_writer_latency = _M._check_state_writer_latency

    def __init__(self, outcome) -> None:  # type: ignore[no-untyped-def]
        self._state_persistence = type("P", (), {"latency_status": lambda self: {}})()
        self._state_writer_alarm = type("A", (), {"observe": lambda self, status: outcome})()
        self.logged: list[tuple[str, str]] = []
        self.paged: list[dict[str, Any]] = []

    def _log(self, level: str, component: str, event: str, **fields: Any) -> None:
        self.logged.append((level, event))

    def _raise_harness_failed_alarm(self, key: str, **kwargs: Any) -> None:
        self.paged.append(kwargs)


def test_slow_logs_an_error_and_pages_the_operator_while_recovery_only_logs() -> None:
    slow = _Daemon(("slow", {"utilization": 0.97}))
    slow._check_state_writer_latency()
    assert slow.logged == [("error", "daemon.state_writer.slow")]
    assert slow.paged and slow.paged[0]["kind"] == "state-writer-slow"
    # Fixed reason so the inbox can collapse repeats; numbers in the text.
    assert slow.paged[0]["reason"] == "STATE_WRITER_SLOW"
    assert "utilization=0.97" in slow.paged[0]["text"]

    recovered = _Daemon(("recovered", {"utilization": 0.1}))
    recovered._check_state_writer_latency()
    assert recovered.logged == [("info", "daemon.state_writer.recovered")]
    assert recovered.paged == []

    cooldown = _Daemon(("slow", {"utilization": 0.91, "_page": False}))
    cooldown._check_state_writer_latency()
    assert cooldown.logged == [("error", "daemon.state_writer.slow")]
    assert cooldown.paged == []
