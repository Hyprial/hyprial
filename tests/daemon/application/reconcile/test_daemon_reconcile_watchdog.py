"""Watchdog + async-start regression cover for the 2026-08-23 IPC wedge.

Postmortem shape being locked down here: a harness whose start never
completes (or fails instantly, like codex on a PATH-less launchd daemon)
must not be able to starve the accept loop, because harness starts used to
run synchronously inside the reconcile tick that shares the accept loop.

Mutation contract (verified during development, see PR evidence):
  * make the supervisor submit starts synchronously again -> test 1 goes red
    (IPC probes time out while the tick is parked in a start);
  * delete the ``daemon.reconcile_overrun`` log block -> test 3 goes red;
  * delete the failure-budget termination -> tests 1/2 go red (retries
    never stop).

The state root lives under /tmp because AF_UNIX paths are length-limited and
pytest's tmp_path on this host exceeds it.
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.runtime import ReconcileSummary
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections


class _FakePresence:
    def online_actors(self) -> tuple[()]:
        return ()

    def actor_online(self, actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[()]:
        return ()


class _FakeHarnesses:
    def status(self) -> tuple[()]:
        return ()


class _HungProcess:
    def __init__(self) -> None:
        self.running = True
        self.pid: int | None = None
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1
        self.running = False


class _NeverReadyLauncher:
    """start() never returns until the test releases it: the injected mutant."""

    def __init__(self) -> None:
        self.attempts = 0
        self.processes: list[_HungProcess] = []
        self.release = threading.Event()

    def start(self, spec: Any) -> _HungProcess:
        self.attempts += 1
        process = _HungProcess()
        self.processes.append(process)
        self.release.wait()
        return process


class _SupervisorRuntime:
    """Minimal runtime seam: one reconcile tick drives the real supervisor."""

    def __init__(self, supervisor: ManagedHarnessRuntime) -> None:
        self.supervisor = supervisor

    def on_timer(self, _observed_at_ms: int | None = None) -> ReconcileSummary:
        started_at = time.monotonic()
        restarts = self.supervisor.reconcile()
        return ReconcileSummary(
            harness_restarts=restarts,
            inbox_results=0,
            harness_failed=self.supervisor.drain_failed_events(),
            phase_ms=(
                ("harnesses.reconcile", int((time.monotonic() - started_at) * 1000)),
            ),
        )

    def stop(self) -> None:
        # DaemonApplication owns the Harness domain directly; this runtime
        # seam must not stop it a second time during teardown.
        pass


def _app(state: Path) -> DaemonApplication:
    (state / "state").mkdir(parents=True)
    (state / "state" / "desired-state.json.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    return DaemonApplication(
        state_dir=state / "state",
        socket_path=state / "state" / "daemon.sock",
        node_id="reconcile-watchdog-test",
        hyprial_home=state / "home",
        **_app_injections(),
    )


def _wire(app: DaemonApplication, root: Path, runtime: Any) -> None:
    app._inbox = InboxService(root / "inbox.sqlite3", MemoryDeliveryTransport())
    app._harnesses = getattr(runtime, "supervisor", None) or _FakeHarnesses()  # type: ignore[assignment]
    app._presence = _FakePresence()  # type: ignore[assignment]
    app._runtime = runtime  # type: ignore[assignment]


def _ipc_ps(socket_path: Path, *, timeout: float = 2.0) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(socket_path))
        client.sendall(
            json.dumps(
                {"version": 1, "id": "probe", "method": "ps", "params": {}}
            ).encode()
            + b"\n"
        )
        return json.loads(client.makefile("rb").readline())
    finally:
        client.close()


def _log_entries(app: DaemonApplication) -> list[dict[str, Any]]:
    log_path = app.state_dir / "logs" / "daemon.jsonl"
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _wait_for(predicate: Any, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"{what} never happened"
        time.sleep(0.02)


def test_never_ready_harness_neither_blocks_accept_nor_retries_forever() -> None:
    """THE acceptance test: accept stays responsive while a harness can never
    start, and the culprit is failed (retries stop) instead of consuming
    one tick-blocking start per pass."""

    root = Path(tempfile.mkdtemp(prefix="hyprial-wd-", dir=hyprial_test_tmp_root()))
    launcher = _NeverReadyLauncher()
    supervisor = ManagedHarnessRuntime(
        launcher,
        restart_backoff_seconds=0.01,
        start_timeout_seconds=0.5,
        failure_budget=2,
    )
    app = _app(root)
    runtime = _SupervisorRuntime(supervisor)
    _wire(app, root, runtime)
    # Startup restore with a hung start must still finish (bounded by the
    # start timeout, not by the hung launcher) and count the failure.
    restore = supervisor.restore(
        (HarnessLaunchSpec("codex", "never-ready", True),)
    )
    assert restore.failed == 1
    assert launcher.attempts == 1

    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        # Probe for at least four seconds AND eight successful requests.
        # Shared CI can deschedule the server for more than two seconds;
        # this is a liveness regression, not a 1.5s latency benchmark.
        # The launcher remains parked until finally, so a genuinely wedged
        # request still fails the bounded socket deadline without retries.
        probe_timeout = 10.0
        probe_deadline = time.monotonic() + 4.0
        probes = 0
        while time.monotonic() < probe_deadline or probes < 8:
            response = _ipc_ps(app.socket_path, timeout=probe_timeout)
            assert response["result"]["daemon"]["running"] is True
            probes += 1
            time.sleep(0.25)
        assert probes >= 8

        # The culprit was failed, by name, and retries stopped.
        _wait_for(
            lambda: any(
                entry.get("event") == "daemon.harness.failed"
                and entry.get("harness") == "codex:never-ready"
                for entry in _log_entries(app)
            ),
            what="harness failed event",
        )
        attempts_at_failure = launcher.attempts
        # U0b (Allen 2026-09-03) retired the OLD assertion
        # `attempts_at_failure == 2`: it pinned the pre-U0b rhythm where the
        # restore failure was retried once by reconcile before budget 2
        # tripped.  A restore-kind start is a single attempt now -- the
        # verdict lands on the first (and only) attempt, which is the
        # sharper form of the property this test pins: retries STOP.
        assert attempts_at_failure == 1
        time.sleep(1.5)
        assert launcher.attempts == attempts_at_failure, (
            "failed harness kept being retried"
        )

        row = next(
            item for item in supervisor.status() if item["id"] == "codex:never-ready"
        )
        assert row["state"] == "failed"
        assert row["quarantined"] is True  # deprecated alias for state failed
        assert row["running"] is False
    finally:
        launcher.release.set()
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()
        # Releasing the hung launcher proves late completions cannot leak a
        # live worker: the abandonment done-callback stops each of them.
        _wait_for(
            lambda: all(p.stops >= 1 for p in launcher.processes),
            what="late-start leak containment",
        )
        shutil.rmtree(root, ignore_errors=True)


def test_fast_failures_stop_after_the_budget() -> None:
    """Unit-level: N consecutive start failures fail the key and emit
    exactly one drainable event; the failing item is never attempted again."""

    class _RefusingLauncher:
        def __init__(self) -> None:
            self.attempts = 0

        def start(self, spec: Any) -> Any:
            self.attempts += 1
            raise RuntimeError("no such binary (fixture)")

    launcher = _RefusingLauncher()
    supervisor = ManagedHarnessRuntime(
        launcher,
        restart_backoff_seconds=0.01,
        start_timeout_seconds=5.0,
        failure_budget=3,
    )
    spec = HarnessLaunchSpec("codex", "refused", True)
    summary = supervisor.restore((spec,))
    assert summary.failed == 1
    assert launcher.attempts == 1

    # U0b (Allen 2026-09-03) retired the OLD assertions "drive ticks until
    # the budget trips" and `launcher.attempts == 3`: they pinned the
    # pre-U0b rhythm where a restore-kind failure retried inside the
    # reconcile budget (three starts before the verdict).  A restore-kind
    # start is a single attempt by ruling: the key is already failed at
    # settlement.  The budget machinery for non-restore kinds is unchanged
    # and stays pinned by tests/test_harness_actor.py (explicit start
    # timeout) and the projection budget tests.
    assert "codex:refused" in supervisor._failed
    assert supervisor.drain_failed_events() == ("codex:refused",)
    assert supervisor.drain_failed_events() == ()  # drained once

    for _ in range(3):
        supervisor.reconcile()
    assert launcher.attempts == 1
    assert "no such binary" in supervisor.last_errors["codex:refused"]
    supervisor.stop()


def test_overrun_tick_is_logged_with_duration_and_culprit(
    monkeypatch: Any,
) -> None:
    """The watchdog names the slow phase; it never kills the serve loop."""

    class _SlowRuntime:
        def __init__(self) -> None:
            self.calls = 0

        def on_timer(self, _observed_at_ms: int | None = None) -> Any:
            self.calls += 1
            time.sleep(0.4)
            return type(
                "Outcome",
                (),
                {
                    "harness_restarts": 0,
                    "inbox_results": 0,
                    "inbox_pruned": 0,
                    "inbox_pruned_items": (),
                    "phase_ms": (("harnesses.reconcile", 401),),
                },
            )()

        def stop(self) -> None:
            pass

    monkeypatch.setenv("HYPRIAL_RECONCILE_TICK_BUDGET", "0.1")
    root = Path(tempfile.mkdtemp(prefix="hyprial-wd-", dir=hyprial_test_tmp_root()))
    app = _app(root)
    runtime = _SlowRuntime()
    _wire(app, root, runtime)
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        _wait_for(lambda: runtime.calls >= 2, what="two reconcile ticks")
        response = _ipc_ps(app.socket_path)
        assert response["result"]["daemon"]["running"] is True
        assert dispatcher.is_alive()  # overrun did not terminate the loop

        overruns = [
            entry
            for entry in _log_entries(app)
            if entry.get("event") == "daemon.reconcile_overrun"
        ]
        assert overruns, "over-budget tick was not reported"
        assert overruns[0]["durationMs"] >= 350
        assert overruns[0]["budgetMs"] == 100
        assert overruns[0]["item"] == "runtime.harnesses.reconcile"
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()
        shutil.rmtree(root, ignore_errors=True)


def test_slow_request_does_not_block_maintenance_scheduler(
    monkeypatch: Any,
) -> None:
    """A parked IPC domain call cannot convoy daemon maintenance.

    The former daemon-wide ``_ipc_request_lock`` linearized every request with
    maintenance, so one slow lifecycle/workflow operation starved adapter
    custody and health timers.  Domain actors now own ordering independently;
    lock the absence of that global authority and the real socket concurrency.
    """

    class _FastRuntime:
        def on_timer(self, _observed_at_ms: int | None = None) -> Any:
            return type(
                "Outcome",
                (),
                {
                    "harness_restarts": 0,
                    "inbox_results": 0,
                    "inbox_pruned": 0,
                    "inbox_pruned_items": (),
                },
            )()

        def stop(self) -> None:
            pass

    root = Path(tempfile.mkdtemp(prefix="hyprial-wd-", dir=hyprial_test_tmp_root()))
    app = _app(root)
    runtime = _FastRuntime()
    runtime.calls = 0
    original_timer = runtime.on_timer

    def counted_timer(observed_at_ms: int | None = None) -> Any:
        runtime.calls += 1
        return original_timer(observed_at_ms)

    runtime.on_timer = counted_timer
    _wire(app, root, runtime)
    request_entered = threading.Event()
    release_request = threading.Event()
    original_handle = app.handle

    def blocking_handle(method: str, params: dict[str, Any], **kwargs: Any) -> Any:
        if method == "test.block":
            request_entered.set()
            assert release_request.wait(5.0)
            return {"released": True}
        return original_handle(method, params, **kwargs)

    app.handle = blocking_handle  # type: ignore[method-assign]
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5.0)
    try:
        assert not hasattr(app, "_ipc_request_lock")
        client.connect(str(app.socket_path))
        client.sendall(
            json.dumps(
                {"version": 1, "id": "blocked", "method": "test.block", "params": {}}
            ).encode()
            + b"\n"
        )
        assert request_entered.wait(2.0)
        calls_at_block = runtime.calls
        _wait_for(lambda: runtime.calls >= calls_at_block + 2, what="maintenance while IPC blocked")
        assert _ipc_ps(app.socket_path)["result"]["daemon"]["running"] is True
        release_request.set()
        assert json.loads(client.makefile("rb").readline())["result"] == {"released": True}
    finally:
        release_request.set()
        client.close()
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()
        shutil.rmtree(root, ignore_errors=True)


def test_restore_runs_starts_concurrently() -> None:
    """Restore wall time is the slowest single start, not the sum (three
    20s-scale codex timeouts must cost one timeout, not three)."""

    class _SlowLauncher:
        def start(self, spec: Any) -> Any:
            time.sleep(0.5)
            return _HungProcess()

    supervisor = ManagedHarnessRuntime(
        _SlowLauncher(),
        start_timeout_seconds=10.0,
    )
    specs = tuple(
        HarnessLaunchSpec("codex", f"slow-{index}", True) for index in range(3)
    )
    started_at = time.monotonic()
    summary = supervisor.restore(specs)
    elapsed = time.monotonic() - started_at
    try:
        assert summary.restored == 3
        assert elapsed < 1.0, f"restore took {elapsed:.2f}s (serial would be 1.5s)"
    finally:
        supervisor.stop()


def test_overrun_tick_names_the_top_offenders_not_just_the_slowest(
    monkeypatch: Any,
) -> None:
    """2026-09-14: the one-item overrun form read as "every step timed out"
    whenever ONE step did (62 runtime.inbox.retry_due overruns looked like a
    fleet-wide stall).  The event now carries the top-3 phase durations.

    Mutation contract: dropping the ``items`` field (or reverting to the
    slowest-only max) makes the assertions below go red.
    """

    class _MultiPhaseRuntime:
        def __init__(self) -> None:
            self.calls = 0

        def on_timer(self, _observed_at_ms: int | None = None) -> Any:
            self.calls += 1
            time.sleep(0.4)
            return type(
                "Outcome",
                (),
                {
                    "harness_restarts": 0,
                    "inbox_results": 0,
                    "inbox_pruned": 0,
                    "inbox_pruned_items": (),
                    "phase_ms": (
                        ("inbox.retry_due", 380),
                        ("harnesses.reconcile", 300),
                        ("harnesses.complete_results", 200),
                        ("harnesses.dispatch_deliveries", 100),
                    ),
                },
            )()

        def stop(self) -> None:
            pass

    monkeypatch.setenv("HYPRIAL_RECONCILE_TICK_BUDGET", "0.1")
    root = Path(tempfile.mkdtemp(prefix="hyprial-wd-", dir=hyprial_test_tmp_root()))
    app = _app(root)
    runtime = _MultiPhaseRuntime()
    _wire(app, root, runtime)
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        _wait_for(lambda: runtime.calls >= 1, what="one reconcile tick")
        _wait_for(
            lambda: any(
                entry.get("event") == "daemon.reconcile_overrun"
                for entry in _log_entries(app)
            ),
            what="overrun event",
        )
        overruns = [
            entry
            for entry in _log_entries(app)
            if entry.get("event") == "daemon.reconcile_overrun"
        ]
        # Backwards-compatible single culprit, plus the top-3 ledger.
        assert overruns[0]["item"] == "runtime.inbox.retry_due"
        items = overruns[0]["items"]
        assert items == [
            {"item": "runtime.inbox.retry_due", "ms": 380},
            {"item": "runtime.harnesses.reconcile", "ms": 300},
            {"item": "runtime.harnesses.complete_results", "ms": 200},
        ]
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()
        shutil.rmtree(root, ignore_errors=True)
