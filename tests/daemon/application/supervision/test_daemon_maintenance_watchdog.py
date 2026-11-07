"""The daemon notices its own maintenance tick stalling, and says where.

2026-09-27 (#890): the maintenance tick ran every few minutes, then stopped
after 04:38:21Z, and the daemon logged nothing for 40 minutes.
``daemon.reconcile_overrun`` is written only when a tick RETURNS, so a tick
that never returns is exactly the case it cannot see.  The watchdog runs on
its own thread and reports a tick that has been running (or a scheduler that
has not started one) for longer than the stall budget: the phase it is in,
the maintenance thread's stack, and the transport lock holder.

Mutation contract: drop the ``phase`` call in ``_timed`` and the wiring test
reports the wrong phase; drop ``tick_started`` from ``_scheduled_maintenance``
and it reports no stall at all.
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from hyprial.shell.impl.cli.commands.doctor.commands import _maintenance_doctor_check
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.lifecycle.maintenance_watchdog import MaintenanceWatchdog
from hyprial.daemon.impl.runtime import ReconcileSummary
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections


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


def _block_in_forwarding_redial(entered: threading.Event, release: threading.Event, watchdog: MaintenanceWatchdog) -> None:
    watchdog.tick_started()
    watchdog.phase("forwarding.timer")
    entered.set()
    release.wait(10)
    watchdog.tick_finished()


def test_a_hung_phase_is_reported_with_its_phase_stack_and_lock_holder() -> None:
    clock, log = _Clock(), _Log()
    holder = {"thread": "hyprial-daemon-ipc-client", "role": "lock", "heldSeconds": 42.0, "stack": "..."}
    watchdog = MaintenanceWatchdog(stall_seconds=60, log=log, diagnostics=lambda: holder, clock=clock)
    entered, release = threading.Event(), threading.Event()
    tick = threading.Thread(
        target=_block_in_forwarding_redial,
        args=(entered, release, watchdog),
        name="daemon-maintenance-test",
        daemon=True,
    )
    tick.start()
    try:
        assert entered.wait(5)
        clock.now += 59
        assert watchdog.check() is None, "reported before the stall budget"

        clock.now += 2
        report = watchdog.check()
        assert report is not None
        assert report["phase"] == "forwarding.timer"
        assert report["thread"] == "daemon-maintenance-test"
        assert "_block_in_forwarding_redial" in report["stack"]
        assert report["transportLock"] == holder
        stalled = log.events("daemon.maintenance.stalled")
        assert len(stalled) == 1
        assert stalled[0]["phase"] == "forwarding.timer"
        assert stalled[0]["elapsedMs"] >= 61_000

        clock.now += 10
        watchdog.check()
        assert len(log.events("daemon.maintenance.stalled")) == 1, "re-logged every poll"
        clock.now += 60
        watchdog.check()
        assert len(log.events("daemon.maintenance.stalled")) == 2, "a long stall must keep saying so"

        status = watchdog.status()
        assert status["stalled"] is True
        assert status["phase"] == "forwarding.timer"
    finally:
        release.set()
        tick.join(5)
    recovered = log.events("daemon.maintenance.recovered")
    assert len(recovered) == 1
    assert watchdog.status()["stalled"] is False


def test_a_scheduler_that_stops_starting_ticks_is_reported() -> None:
    clock, log = _Clock(), _Log()
    watchdog = MaintenanceWatchdog(stall_seconds=60, log=log, diagnostics=lambda: None, clock=clock)
    assert watchdog.check() is None, "no tick has ever run: startup is not a stall"
    watchdog.tick_started()
    watchdog.tick_finished()
    clock.now += 61
    report = watchdog.check()
    assert report is not None
    assert report["phase"] == "between-ticks"
    assert log.events("daemon.maintenance.stalled")


def test_the_real_tick_reports_the_phase_it_hangs_in() -> None:
    root = Path(tempfile.mkdtemp(prefix="mwd-", dir=hyprial_test_tmp_root()))
    try:
        (root / "state").mkdir()
        (root / "state" / "desired-state.json.v1").write_text(
            json.dumps({"schemaVersion": 1, "asMailbox": False, "providers": [], "interactiveSessions": []}),
            encoding="utf-8",
        )
        app = DaemonApplication(
            state_dir=root / "state",
            socket_path=root / "state" / "daemon.sock",
            node_id="maintenance-watchdog-test",
            hyprial_home=root / "home",
        **_app_injections(),
        )
        entered, release = threading.Event(), threading.Event()

        class _HangingRuntime:
            def on_timer(self, _observed_at_ms: int | None = None) -> ReconcileSummary:
                entered.set()
                release.wait(10)
                return ReconcileSummary(harness_restarts=0, inbox_results=0)

            def stop(self) -> None:
                pass

        app._inbox = InboxService(root / "inbox.sqlite3", MemoryDeliveryTransport())
        app._runtime = _HangingRuntime()  # type: ignore[assignment]
        app._maintenance_generation = 1
        tick = threading.Thread(target=app._scheduled_maintenance, args=(1,), name="tick-under-test", daemon=True)
        tick.start()
        try:
            assert entered.wait(10), "the tick never reached the runtime timer"
            report = app._maintenance_watchdog.check(now=time.monotonic() + 3_600)
            assert report is not None, "the daemon's tick is not wired to the watchdog"
            assert report["phase"] == "runtime.timer"
            assert report["thread"] == "tick-under-test"
            assert "on_timer" in report["stack"]
        finally:
            app.stop_event.set()
            release.set()
            tick.join(10)
            app._close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_doctor_fails_on_a_stalled_tick_and_passes_otherwise() -> None:
    assert _maintenance_doctor_check({}) is None, "an older daemon without the field"
    ok = _maintenance_doctor_check({"maintenance": {"stalled": False}})
    assert ok is not None and ok["status"] == "ok"
    failed = _maintenance_doctor_check(
        {"maintenance": {"stalled": True, "phase": "forwarding.timer", "stalledSeconds": 95.0}}
    )
    assert failed is not None
    assert failed["status"] == "fail"
    assert "forwarding.timer" in failed["detail"]
