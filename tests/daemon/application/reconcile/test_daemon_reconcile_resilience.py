"""A failing maintenance tick must never take the daemon down.

Regression cover for the scheduled domain cadence boundary in
``_serve``: before the guard, any transient exception inside the periodic
reconcile (e.g. from the TTL-expiry path) unwound the accept loop and shut
the whole daemon down, closing accepted IPC clients mid-request -- the
DAEMON_DISCONNECTED shape behind the E2E-002 gate flake.

These tests use a real socket and the real serve loop.  The state root lives
under /tmp because AF_UNIX paths are length-limited and pytest's tmp_path on
this host exceeds it.
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

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    MemoryDeliveryTransport,
)
from hyprial.biz.impl.routine.execution.service import RoutineService


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections

#: How long `routine.remove` may take while the source query is blocked.  It
#: bounds a hang, ⛔ not the machine: the assertion that follows it is about
#: what remove waited FOR, never about how fast this runner is.
REMOVE_WAIT_SECONDS = 30.0


class _FakeHarnesses:
    def status(self) -> tuple[()]:
        return ()


class _FakePresence:
    def online_actors(self) -> tuple[()]:
        return ()

    def actor_online(self, actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[()]:
        return ()


class _FakeAdapters:
    gateway_names: tuple[()] = ()

    def drain_health_events(self) -> tuple[()]:
        return ()

    def reconcile(self) -> int:
        return 0

    def stop(self) -> None:
        pass


class _InboxEvents:
    def publish(self, _event: object) -> None:
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
        node_id="reconcile-resilience-test",
        hyprial_home=state / "home",
        **_app_injections(),
    )


def _wire(app: DaemonApplication, root: Path) -> None:
    database = root / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database,
        MemoryDeliveryTransport(),
        _InboxEvents(),
    )
    app._inbox_coordinator = coordinator
    app._inbox = DeliveryCustodyFacade(coordinator, database)
    app._harnesses = _FakeHarnesses()  # type: ignore[assignment]
    app._presence = _FakePresence()  # type: ignore[assignment]


class _RaisingRuntime:
    def __init__(self) -> None:
        self.calls = 0

    def on_timer(self, _observed_at_ms: int | None = None) -> Any:
        self.calls += 1
        raise RuntimeError("fixture reconcile failure")

    def stop(self) -> None:
        pass


class _WorkingRuntime:
    """A runtime timer that always succeeds, so the workflow timer below
    is the only thing failing inside ``_run_scheduled_domains``."""

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


class _RaisingWorkflowService:
    """Stands in for a Workflow timer admission failure."""

    def __init__(self) -> None:
        self.calls = 0

    def submit_timer(self, _observed_at_ms: int | None = None) -> None:
        self.calls += 1
        raise RuntimeError("fixture workflow tick failure")

    def close(self) -> None:
        pass


class _BlockingWorkflowService:
    """A slow external report attempt behind Workflow timer admission."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def submit_timer(self, _observed_at_ms: int | None = None) -> None:
        self.started.set()
        assert self.release.wait(2.0)

    def close(self) -> None:
        pass


class _ConvoyWorkflowService:
    """Model a domain timer holding its lock while workflow.status waits."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.tick_started = threading.Event()
        self.status_started = threading.Event()
        self.release_tick = threading.Event()

    def submit_timer(self, _observed_at_ms: int | None = None) -> None:
        with self._lock:
            self.tick_started.set()
            assert self.release_tick.wait(2.0)

    def status(self, *, run_id: str) -> dict[str, object]:
        self.status_started.set()
        with self._lock:
            return {"runId": run_id, "state": "running"}

    def close(self) -> None:
        pass


class _RoutinePac:
    """Inert PAC seam: this file's subject is lock convoying, not dispatch."""

    def start_idempotent(self, **kwargs: object) -> dict[str, object]:
        return {"graphId": "unused", "state": "running"}

    def status(self, *, graph_id: str) -> dict[str, object]:
        return {"graphId": graph_id, "state": "running"}

    def close(self, *, graph_id: str, actor: str) -> None:
        pass


class _RoutineAlarm:
    def escalate(self, *, to: str, text: str, reason: str | None = None) -> None:
        pass


_ROUTINE_YAML = """
version: 1
name: lock-regression
schedule: {interval: 1m}
source: {kind: taskwarrior, filter: "+selfdrive"}
policy:
  routes:
    - {tag: "route:self", target: "self"}
  default: escalate
  task_template: "{{task.description}}"
limits:
  max_in_flight: 1
  circuit_breaker: {window_runs: 2, escalate_ratio: 1.0, action: "pause+alarm"}
on_task_timeout: {action: escalate, escalate_to: "user:allen"}
"""


def _ipc_call(
    socket_path: Path,
    method: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 2.0,
) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(socket_path))
        client.sendall(
            json.dumps(
                {
                    "version": 1,
                    "id": "probe",
                    "method": method,
                    "params": params or {},
                }
            ).encode()
            + b"\n"
        )
        return json.loads(client.makefile("rb").readline())
    finally:
        client.close()


def _ipc_ps(socket_path: Path, *, timeout: float = 2.0) -> dict[str, Any]:
    return _ipc_call(socket_path, "ps", timeout=timeout)


def test_failed_reconcile_tick_is_logged_and_the_daemon_survives() -> None:
    root = Path(tempfile.mkdtemp(prefix="hyprial-rc-", dir=hyprial_test_tmp_root()))
    app = _app(root)
    _wire(app, root)
    runtime = _RaisingRuntime()
    app._runtime = runtime  # type: ignore[assignment]
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        # The first reconcile fires ~1s into the serve loop and every second
        # after that; give it room for several failing ticks.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and runtime.calls < 2:
            time.sleep(0.05)
        assert runtime.calls >= 2, "reconcile ticks never ran"

        # The daemon is still serving real IPC after repeated failures.
        response = _ipc_ps(app.socket_path)
        assert response["id"] == "probe"
        assert response["result"]["daemon"]["running"] is True
        assert dispatcher.is_alive()

        # And every failure was logged, not swallowed silently.
        log_path = app.state_dir / "logs" / "daemon.jsonl"
        entries = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        failures = [
            entry for entry in entries if entry.get("event") == "daemon.reconcile_failed"
        ]
        assert failures, "reconcile failure was not logged"
        assert failures[0]["errorType"] == "RuntimeError"
        assert "fixture reconcile failure" in failures[0]["detail"]
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()
        app.agents.close()
        shutil.rmtree(root, ignore_errors=True)


def test_failed_workflow_tick_is_logged_and_the_daemon_survives() -> None:
    """A workflow tick crash is logged and cannot unwind the accept loop."""

    root = Path(tempfile.mkdtemp(prefix="hyprial-rc-wf-", dir=hyprial_test_tmp_root()))
    app = _app(root)
    _wire(app, root)
    app._runtime = _WorkingRuntime()  # type: ignore[assignment]
    workflow_service = _RaisingWorkflowService()
    app._workflow_service = workflow_service  # type: ignore[assignment]
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and workflow_service.calls < 2:
            time.sleep(0.05)
        assert workflow_service.calls >= 2, "workflow tick never ran"

        response = _ipc_ps(app.socket_path)
        assert response["id"] == "probe"
        assert response["result"]["daemon"]["running"] is True
        assert dispatcher.is_alive()

        log_path = app.state_dir / "logs" / "daemon.jsonl"
        entries = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        failures = [
            entry for entry in entries if entry.get("event") == "daemon.reconcile_failed"
        ]
        assert failures, "workflow tick failure was not logged"
        assert failures[0]["errorType"] == "RuntimeError"
        assert "fixture workflow tick failure" in failures[0]["detail"]
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()
        app.agents.close()
        shutil.rmtree(root, ignore_errors=True)


def test_slow_workflow_tick_does_not_block_daemon_ipc() -> None:
    """A PAC report may wait seconds for a real user-delivery receipt.

    That wait must not hold the daemon-wide IPC linearization lock: Lark
    inbound needs ``adapter.pins`` and ``message.send`` before it can add its
    custody reaction, and blocking those calls turns a slow workflow report
    into a missing/late ACK on every adapter.
    """

    root = Path(tempfile.mkdtemp(prefix="hyprial-rc-wf-ipc-", dir=hyprial_test_tmp_root()))
    app = _app(root)
    _wire(app, root)
    app._runtime = _WorkingRuntime()  # type: ignore[assignment]
    app._adapters = _FakeAdapters()  # type: ignore[assignment]
    workflow_service = _BlockingWorkflowService()
    app._workflow_service = workflow_service  # type: ignore[assignment]
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    periodic = threading.Thread(
        target=app._run_scheduled_domains,
        args=(time.monotonic(),),
        daemon=True,
    )
    periodic.start()
    try:
        assert workflow_service.started.wait(1.0), "workflow tick never started"
        started_at = time.monotonic()
        response = _ipc_call(app.socket_path, "adapter.pins", timeout=0.5)
        assert response["result"] == {"ok": True, "supportsExpectedActor": True, "pins": {}}
        assert time.monotonic() - started_at < 0.5
    finally:
        workflow_service.release.set()
        periodic.join(timeout=1.0)
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()
        app.agents.close()
        shutil.rmtree(root, ignore_errors=True)


def test_workflow_status_waiting_for_tick_does_not_convoy_adapter_ipc() -> None:
    """A service-lock waiter must not retain the daemon-wide IPC lock."""

    root = Path(tempfile.mkdtemp(prefix="hyprial-rc-wf-convoy-", dir=hyprial_test_tmp_root()))
    app = _app(root)
    _wire(app, root)
    app._runtime = _WorkingRuntime()  # type: ignore[assignment]
    app._adapters = _FakeAdapters()  # type: ignore[assignment]
    workflow_service = _ConvoyWorkflowService()
    app._workflow_service = workflow_service  # type: ignore[assignment]
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    periodic = threading.Thread(
        target=app._run_scheduled_domains,
        args=(time.monotonic(),),
        daemon=True,
    )
    periodic.start()
    status_result: dict[str, Any] = {}

    def request_status() -> None:
        status_result.update(
            _ipc_call(
                app.socket_path,
                "workflow.status",
                params={"runId": "run-blocked", "actor": f"user:{app.owner}"},
            )
        )

    status_thread = threading.Thread(target=request_status, daemon=True)
    try:
        assert workflow_service.tick_started.wait(1.0), "workflow tick never started"
        status_thread.start()
        assert workflow_service.status_started.wait(1.0), "workflow.status never entered"
        response = _ipc_call(app.socket_path, "adapter.pins", timeout=0.5)
        assert response["result"] == {"ok": True, "supportsExpectedActor": True, "pins": {}}
    finally:
        workflow_service.release_tick.set()
        status_thread.join(timeout=1.0)
        periodic.join(timeout=1.0)
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()
        app.agents.close()
        shutil.rmtree(root, ignore_errors=True)
    assert status_result["result"]["runId"] == "run-blocked"


def test_routine_remove_bypasses_external_io_and_cannot_be_resurrected() -> None:
    """Source I/O is outside the actor; its stale completion cannot resurrect."""

    root = Path(tempfile.mkdtemp(prefix="hyprial-rc-routine-lock-", dir=hyprial_test_tmp_root()))
    app = _app(root)
    _wire(app, root)
    app._runtime = _WorkingRuntime()  # type: ignore[assignment]
    app._adapters = _FakeAdapters()  # type: ignore[assignment]
    clock = {"now": 0}
    source_started = threading.Event()
    release_source = threading.Event()

    def blocked_source(_filter: str) -> list[Any]:
        source_started.set()
        assert release_source.wait(2.0)
        return []

    routine_service = RoutineService(
        pac=_RoutinePac(),  # type: ignore[arg-type]
        alarm=_RoutineAlarm(),
        state_dir=root,
        clock_ms=lambda: clock["now"],
        source_query=blocked_source,
    )
    routine_service.add(yaml_text=_ROUTINE_YAML, owner="agent:o:n:duty")
    clock["now"] = 60_001
    app._routine_service = routine_service
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    periodic = threading.Thread(
        target=app._run_scheduled_domains,
        args=(time.monotonic(),),
        daemon=True,
    )
    periodic.start()
    remove_result: dict[str, Any] = {}

    def remove_routine() -> None:
        remove_result.update(
            _ipc_call(
                app.socket_path,
                "routine.remove",
                params={"name": "lock-regression", "actor": f"user:{app.owner}"},
            )
        )

    remove_thread = threading.Thread(target=remove_routine, daemon=True)
    try:
        assert source_started.wait(1.0), "routine source query never started"
        remove_thread.start()
        # ⛔ NOT a 50ms sleep: that gave a full IPC round trip a budget measured
        # in machine speed, and a throttled 2-CPU runner blows it (task 16430).
        # The claim here is an ORDERING one and needs no clock: `release_source`
        # is still unset, so the source query is STILL BLOCKED -- completing at
        # all is what proves `routine.remove` did not wait for it.  A generous
        # wait can only make the test slower, never wronger.
        remove_thread.join(timeout=REMOVE_WAIT_SECONDS)
        assert not remove_thread.is_alive(), "routine.remove waited for external source I/O"
        release_source.set()
        remove_thread.join(timeout=1.0)
        periodic.join(timeout=1.0)
        assert not remove_thread.is_alive(), "routine.remove did not complete"
        assert remove_result["result"] == {
            "name": "lock-regression",
            "removed": True,
        }
        listed = _ipc_call(app.socket_path, "routine.list", params={"actor": f"user:{app.owner}"})
        assert listed["result"] == {"routines": []}
    finally:
        release_source.set()
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()
        app.agents.close()
        shutil.rmtree(root, ignore_errors=True)


class _RecordingOrgFs:
    def __init__(self) -> None:
        self.ticks: list[float] = []

    def anti_entropy_tick(self, now: float) -> int:
        self.ticks.append(now)
        return 0

    def close(self) -> None:
        pass


class _RecordingAdapters(_FakeAdapters):
    def __init__(self) -> None:
        self.drained = 0

    def drain_health_events(self) -> tuple[()]:
        self.drained += 1
        return ()

    def timer(self, _observed_at_ms: int) -> Any:
        return type("Outcome", (), {"changed": 0})()


def test_maintenance_ticks_orgfs_anti_entropy_each_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2 wiring: each maintenance pass ticks anti-entropy, then runs the adapter phases.

    The tick itself never raises (see the orgfs anti-entropy tests).
    """

    root = Path(tempfile.mkdtemp(prefix="hyprial-rc-ae-", dir=hyprial_test_tmp_root()))
    app = _app(root)
    _wire(app, root)
    app._runtime = _WorkingRuntime()  # type: ignore[assignment]
    orgfs = _RecordingOrgFs()
    adapters = _RecordingAdapters()
    app._orgfs_runtime = orgfs  # type: ignore[assignment]
    app._lark_client = adapters  # type: ignore[assignment]
    events: list[str] = []
    monkeypatch.setattr(
        app, "_log", lambda _level, _component, event, **_fields: events.append(event)
    )
    try:
        app._run_scheduled_domains(1234.5)

        assert orgfs.ticks == [1234.5]
        assert adapters.drained == 1
        assert "orgfs.anti_entropy.failed" not in events
    finally:
        monkeypatch.undo()
        app._close()
        app.agents.close()
        shutil.rmtree(root, ignore_errors=True)
