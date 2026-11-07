"""Review fixes for the forwarding supervisor: wedged children, honesty, races.

Five findings from the 460e421c review (hyprial-developer, 2026-09-15):

* **B** -- a sidecar that lives but cannot answer ``status`` (the Go
  ``CONTROL_UNREACHABLE`` / "cannot read node status" shape) used to emit one
  ``zenoh.forwarding.failed`` per reconcile tick while reporting ``running``.
  Now: one event per state change (``degraded``), and a wedged child that
  stays wedged is closed and booked against the registered budget.
* **D** -- Zenoh fixes its connect set at session open, so a relaunch with a
  changed local-port set leaves the session dialing dead ports while the
  status said ``running``. Now the status block reports effective vs dialed
  endpoints plus ``restartRequired``.
* **A** -- the first-start race now has its Go wire shape: the sidecar's
  first ``up`` answers ``CONTROL_UNREACHABLE`` "Running status is incomplete"
  and dies; the relaunch is ready. Plus a real SIGKILL cell whose post
  recovery ticks are silent.
* **C** -- an exit landing between the controller handshake and the backend
  install used to get the dead child installed as ``running``. The exit
  callback now carries its incarnation's identity.
* **E** -- the ``forwarding`` block is pinned through the IPC dispatcher
  (``ping``), not by calling the helper directly.

Every daemon here pins ``HYPRIAL_PEER_DISCOVERY=0`` (or a loopback
``HYPRIAL_PEER_DISCOVERY_COMMAND``): a test daemon must never reach the
tailscale backend or the production mesh.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.kernel.impl.actor_runtime.policies import DEFAULT_POLICIES, PROCESS_LIFECYCLE
from hyprial.kernel.impl.actor_runtime.scheduler import GenerationScheduler
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.forwarding import ForwardingSidecarSupervisor
from hyprial.daemon.impl.forwarding.slots import slot_endpoints
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

FIXTURE = Path(__file__).resolve().parents[4] / 'tests/fixtures/network/fake_forwarding_sidecar.py'
_POLICY = DEFAULT_POLICIES[PROCESS_LIFECYCLE]


def _slot_set(tmp_path: Path) -> list[str]:
    """The fixed 2-slot connect set the forwarding half dials."""

    return list(slot_endpoints(tmp_path / "home", 2))

#: The org-directory view the daemon-side tests pin: two members, self excluded.
DIRECTORY = {
    "100.64.20.2": "tcPeerTwo",
    "100.64.20.3": "tcPeerThree",
}


class _RecordingTimer:
    """``threading.Timer``'s shape; the test decides when it fires."""

    daemon = True

    def __init__(self, delay: float, function: Any, args: tuple[Any, ...] = ()) -> None:
        self.delay = delay
        self._function = function
        self._args = args
        self.fired = False
        self.cancelled = False

    def start(self) -> None:  # pragma: no cover - never started, only fired
        pass

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.fired = True
        self._function(*self._args)


class _TimerRecorder:
    def __init__(self) -> None:
        self.timers: list[_RecordingTimer] = []

    def __call__(self, delay: float, function: Any, args: tuple[Any, ...] = ()) -> Any:
        timer = _RecordingTimer(delay, function, args)
        self.timers.append(timer)
        return timer


class _Controller:
    """The pinned controller stub shape (see test_daemon_discovery.py)."""

    def __init__(self) -> None:
        self.mappings = {"100.64.20.2": 41002, "100.64.20.3": 41003}
        self.closed = False

    def status(self) -> tuple[tuple[str, ...], dict[str, str]]:
        return (), {}

    def map_peer(self, peer: str, address: str, slot: str) -> str:
        raise AssertionError("stub is fully mapped; map_peer must not be called")

    def unmap_peer(self, peer: str) -> None:
        self.mappings.pop(peer, None)

    def close(self) -> None:
        self.closed = True


def _app(
    tmp_path: Path,
    monkeypatch,
    *,
    extra_env: dict[str, str] | None = None,
) -> DaemonApplication:
    monkeypatch.setenv(
        "HYPRIAL_FORWARDING_COMMAND",
        json.dumps([sys.executable, str(FIXTURE)]),
    )
    monkeypatch.setenv(
        "HYPRIAL_FORWARDING_UP",
        json.dumps(
            {
                "v": 6, "requestId": "up-fixture",
                "op": "up",
                "keyFile": str(tmp_path / "device-key.json"),
                "membershipStore": str(tmp_path / 'membership.json'),
                "owner": "fixture", "deviceId": "device", "controlPort": 7448,
                "addressFile": str(tmp_path / "state/tailcat/address"),
                "inboundTarget": 'unix:' + str(tmp_path / 'zenoh.sock'),
                "peerPort": 7447,
            }
        ),
    )
    monkeypatch.setenv("FAKE_FORWARD_RECEIPT", str(tmp_path / "receipt.jsonl"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    # A small fixed pool so the dialed slot set is short and assertable.
    monkeypatch.setenv("HYPRIAL_ZENOH_SLOT_POOL", "2")
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return DaemonApplication(
        state_dir=tmp_path / "daemon-state",
        socket_path=tmp_path / "daemon-state/daemon.sock",
        node_id="review-node",
        hyprial_home=tmp_path / "home",
        owner="tester",
    **_deep_module_daemon_dependencies())


def _watch(app: DaemonApplication, monkeypatch) -> list[tuple[str, dict[str, object]]]:
    captured: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        app,
        "_log",
        lambda _level, _component, event, **fields: captured.append((event, fields)),
    )
    return captured


def _pin_directory(app: DaemonApplication, monkeypatch) -> None:
    """The org-directory view the supervisor's backend reconciles against."""

    monkeypatch.setattr(app, "_tailcat_directory_peers", lambda: DIRECTORY)


def _drive_reconcile_until(
    app: DaemonApplication, want_state: str, deadline_seconds: float = 20.0
) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        app._reconcile_forwarding_endpoints()
        if app._forwarding_status_json()["state"] == want_state:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"forwarding never reached {want_state!r}: {app._forwarding_status_json()}"
    )


def _wait_recovery(
    events: list[tuple[str, dict[str, object]]], deadline_seconds: float = 12.0
) -> None:
    """Wait for the started event, not the ``running`` state: right after a
    kill the state still reads ``running`` from the not-yet-reaped child, so
    only the recovery event marks a genuinely new incarnation."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if any(event == "zenoh.forwarding.started" for event, _fields in events):
            return
        time.sleep(0.05)
    raise AssertionError("forwarding never reported a recovered start")


# -- B: wedged child -------------------------------------------------------


def test_wedged_status_channel_degrades_once_then_relaunches_within_budget(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app(tmp_path, monkeypatch, extra_env={"FAKE_FORWARD_STATUS_BROKEN": "1"})
    events = _watch(app, monkeypatch)
    try:
        # A wedged status channel logs the fault but does not empty the
        # connect set: the fixed slot set is returned regardless (design §5).
        assert app._discover_peer_endpoints() == tuple(_slot_set(tmp_path))

        def failed_events() -> list[str]:
            return [
                event for event, _fields in events if event == "zenoh.forwarding.failed"
            ]

        # The review probe's defect: 20 reconcile ticks used to produce 21
        # failure events with the state pinned at "running".
        assert len(failed_events()) == 1
        assert app._forwarding_status_json()["state"] == "degraded"
        for _ in range(20):
            app._reconcile_forwarding_endpoints()
        assert len(failed_events()) == 1, "events must follow state changes, not ticks"
        assert app._forwarding_status_json()["state"] in ("degraded", "restarting")

        # A wedged child that stays wedged is closed and booked against the
        # registered budget: two relaunches, then terminal.
        _drive_reconcile_until(app, "failed")
        assert len(failed_events()) == 1 + _POLICY.max_restarts + 1
        restarting = [
            event for event, _fields in events if event == "zenoh.forwarding.restarting"
        ]
        assert len(restarting) == _POLICY.max_restarts
    finally:
        app._close()


def test_wedged_child_that_recovers_reports_recovery_once(
    tmp_path: Path, monkeypatch
) -> None:
    # Broken status channel that heals: the fixture reads the knob per op, so
    # unsetting it mid-flight is enough -- exercised through the supervisor's
    # own hooks rather than a second fixture mode.
    events: list[tuple[str, dict[str, Any]]] = []
    recorder = _TimerRecorder()
    supervisor = ForwardingSidecarSupervisor(
        {},
        event_log=lambda level, event, **fields: events.append((level, event, fields)),
        controller_factory=lambda environ, on_exit: _Controller(),
        scheduler=GenerationScheduler(timer_factory=recorder),
    )
    supervisor.ensure_started()
    backend = supervisor.endpoints()
    assert backend is not None
    backend._on_failure("cannot read node status")  # type: ignore[attr-defined]
    backend._on_failure("cannot read node status")  # type: ignore[attr-defined]
    assert supervisor.state == "degraded"
    assert [(level, event) for level, event, _f in events] == [
        ("error", "zenoh.forwarding.failed")
    ]
    backend._on_success()  # type: ignore[attr-defined]
    assert supervisor.state == "running"
    assert [(level, event) for level, event, _f in events][-1] == (
        "info",
        "zenoh.forwarding.recovered",
    )
    supervisor.close()


# -- D: effective vs dialed ------------------------------------------------
#
# The legacy per-peer-loopback-port churn across relaunch (ports change ->
# restartRequired) cannot happen with the fixed slot pool: the connect set
# is derived from the home and is identical across relaunch and first-start
# failure.  That invariance is pinned by the supervisor's real-relaunch test
# (tests/daemon/forwarding/supervisor, which asserts the same slot set before
# and after a SIGKILL) and by the slots suite's restart test; the three
# old-path tests that lived here (ports-stable, ports-changed, first-start
# dialed-empty-gap) are removed with their premise.


def test_sigkill_relaunch_keeps_the_fixed_slot_set_dialed(
    tmp_path: Path, monkeypatch
) -> None:
    """The slot invariant: a relaunch never changes the connect set, so
    ``restartRequired`` stays False and ``dialed`` never drifts."""

    app = _app(tmp_path, monkeypatch)
    events = _watch(app, monkeypatch)
    _pin_directory(app, monkeypatch)
    try:
        assert app._discover_peer_endpoints() == tuple(_slot_set(tmp_path))
        pid = app._forwarding_status_json()["pid"]
        assert isinstance(pid, int)
        os.kill(pid, signal.SIGKILL)
        _wait_recovery(events)
        status = app._forwarding_status_json()
        assert status["state"] == "running"
        assert status["endpoints"] == _slot_set(tmp_path)
        assert status["dialed"] == _slot_set(tmp_path)
        assert status["restartRequired"] is False
    finally:
        app._close()


# -- A: Go wire shapes ------------------------------------------------------


def test_go_shaped_first_up_error_is_start_failed_then_recovers(
    tmp_path: Path, monkeypatch
) -> None:
    marker = tmp_path / "first-up-error"
    app = _app(
        tmp_path,
        monkeypatch,
        extra_env={"FAKE_FORWARD_UP_ERROR_ONCE": str(marker)},
    )
    events = _watch(app, monkeypatch)
    try:
        # The first up errors and the child dies; the connect set is still
        # the derivable fixed slot set, never empty.
        assert app._discover_peer_endpoints() == tuple(_slot_set(tmp_path))
        start_failed = [
            fields
            for event, fields in events
            if event == "zenoh.forwarding.start_failed"
        ][0]
        # The controller parsed the sidecar's error EVENT: the Go failure
        # shape, not a Python-side guess.
        assert start_failed["errorType"] == "ForwardingSidecarError"
        assert "START_FAILED" in start_failed["detail"]
        assert "Running status is incomplete" in start_failed["detail"]
        _wait_recovery(events)
        names = [event for event, _fields in events]
        assert "zenoh.forwarding.restarting" in names
        assert "zenoh.forwarding.started" in names
    finally:
        app._close()


def test_real_sigkill_relaunch_is_silent_after_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app(tmp_path, monkeypatch)
    events = _watch(app, monkeypatch)
    _pin_directory(app, monkeypatch)
    try:
        assert app._discover_peer_endpoints() == tuple(_slot_set(tmp_path))
        pid = app._forwarding_status_json()["pid"]
        os.kill(pid, signal.SIGKILL)
        _wait_recovery(events)
        # Post-recovery ticks on a healthy child are silent: failure events
        # follow state changes, and there was no state change to report.
        for _ in range(10):
            app._reconcile_forwarding_endpoints()
        assert app._forwarding_status_json()["state"] == "running"
        assert [
            event for event, _fields in events if event == "zenoh.forwarding.failed"
        ] == []
        assert [
            event for event, _fields in events if event == "zenoh.forwarding.started"
        ]
    finally:
        app._close()


# -- C: pre-install exit identity -------------------------------------------


def test_pre_install_exit_is_not_installed_as_running() -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []
    recorder = _TimerRecorder()
    made: list[_Controller] = []

    def factory(environ, on_exit):
        controller = _Controller()
        made.append(controller)
        if len(made) == 1:
            # The monitor fires in the window between the handshake and the
            # supervisor installing the backend (review finding C's probe).
            on_exit(9)
        return controller

    supervisor = ForwardingSidecarSupervisor(
        {},
        event_log=lambda level, event, **fields: events.append((level, event, fields)),
        controller_factory=factory,
        scheduler=GenerationScheduler(timer_factory=recorder),
    )
    supervisor.ensure_started()

    # The dead child must NOT be installed as running (the pre-fix bug also
    # logged "started" and left the relaunch generation forever stale).
    assert supervisor.endpoints() is None
    assert supervisor.state == "restarting"
    assert made[0].closed is True
    assert [(level, event) for level, event, _f in events] == [
        ("error", "zenoh.forwarding.exited"),
        ("warn", "zenoh.forwarding.restarting"),
    ]
    assert recorder.timers and not recorder.timers[0].fired

    recorder.timers[0].fire()
    assert len(made) == 2
    assert supervisor.state == "running"
    assert supervisor.endpoints() is not None
    assert [(level, event) for level, event, _f in events][-1] == (
        "info",
        "zenoh.forwarding.started",
    )
    supervisor.close()


def test_stale_incarnation_monitor_is_ignored() -> None:
    """A replaced incarnation's late monitor must be ignored wholesale.

    The supervisor stays OPEN and the generation actually advances: child 1
    exits through its own callback, the relaunch timer fires, child 2 is
    installed as running -- and only THEN child 1's monitor fires late (its
    wait thread reaped the corpse only now).  Closing the supervisor first
    (the previous shape) exercised only the ``self._closed`` early return,
    never the stale-incarnation branch: deleting ``if not installed: return``
    from ``_note_exit`` stayed green.  This shape pins the branch itself.
    """

    events: list[tuple[str, str, dict[str, Any]]] = []
    recorder = _TimerRecorder()
    callbacks: list[Any] = []
    made: list[_Controller] = []

    def factory(environ, on_exit):
        callbacks.append(on_exit)
        controller = _Controller()
        made.append(controller)
        return controller

    supervisor = ForwardingSidecarSupervisor(
        {},
        event_log=lambda level, event, **fields: events.append((level, event, fields)),
        controller_factory=factory,
        scheduler=GenerationScheduler(timer_factory=recorder),
    )
    supervisor.ensure_started()
    assert supervisor.state == "running"
    assert len(made) == 1

    # Child 1's real exit: its own callback, a scheduled relaunch, then the
    # timer installs child 2 as the running backend (generation advanced).
    callbacks[0](9)
    assert supervisor.endpoints() is None
    assert supervisor.state == "restarting"
    assert [(level, event) for level, event, _f in events] == [
        ("error", "zenoh.forwarding.exited"),
        ("warn", "zenoh.forwarding.restarting"),
    ]
    recorder.timers[0].fire()
    assert len(made) == 2
    second = supervisor.endpoints()
    assert second is not None
    assert supervisor.state == "running"
    assert [(level, event) for level, event, _f in events][-1] == (
        "info",
        "zenoh.forwarding.started",
    )

    events_before = list(events)
    failures_before = supervisor.failures
    timers_before = len(recorder.timers)

    # The stale callback: child 1's monitor, one generation too late.
    callbacks[0](0)

    # Nothing may move: child 2 stays installed and running, the event table
    # gains no exited/failed/restarting (or anything at all), the failure
    # count is unchanged, and no new relaunch timer is booked.
    assert supervisor.endpoints() is second
    assert supervisor.state == "running"
    assert events == events_before
    assert supervisor.failures == failures_before
    assert len(recorder.timers) == timers_before
    supervisor.close()


# -- E: the IPC dispatcher carries the forwarding block ---------------------


class _InboxStub:
    """Only the reads the ps handler performs on the inbox."""

    def pending_all(self) -> tuple[()]:
        return ()

    def pending_recipient_counts(self) -> list[tuple[str, int]]:
        return []

    def outbox_count(self) -> int:
        return 0

    def custody_count(self) -> int:
        return 0


class _WiringStub:
    """Satisfies only ``handle``'s wiring asserts and permissive reads."""

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: ()


@pytest.mark.parametrize("method", ["ping", "ps"])
def test_ipc_handlers_carry_the_forwarding_block(tmp_path, monkeypatch, method) -> None:
    """Both payloads, through the dispatcher, never by calling the helper.

    The two payloads were pinned separately after a mutation that dropped
    the ps copy alone stayed green: a single-site pin cannot see the other
    site disappear.
    """

    app = _app(tmp_path, monkeypatch)
    _pin_directory(app, monkeypatch)
    app._inbox = _InboxStub()  # type: ignore[assignment]
    app._harnesses = _WiringStub()  # type: ignore[assignment]
    app._presence = _WiringStub()  # type: ignore[assignment]
    try:
        assert app._discover_peer_endpoints() == tuple(_slot_set(tmp_path))
        payload = app.handle(method, {})
        forwarding = payload["forwarding"]
        assert forwarding["state"] == "running"
        assert forwarding["failures"] == 0
        assert isinstance(forwarding["pid"], int)
        assert forwarding["endpoints"] == _slot_set(tmp_path)
        assert forwarding["dialed"] == _slot_set(tmp_path)
        assert forwarding["restartRequired"] is False
    finally:
        app._close()
