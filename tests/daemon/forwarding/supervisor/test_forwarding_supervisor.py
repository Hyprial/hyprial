"""The forwarding sidecar's bounded relaunch: budget, events, status.

A forwarding hub whose sidecar died once used to stay offline until the whole
daemon restarted (`_forwarding_start_attempted` was one-shot). These tests
pin the replacement: a supervisor that relaunches under the registered
process-lifecycle budget (``actor_runtime``'s ``PROCESS_LIFECYCLE``
``SupervisionPolicy`` -- max_restarts=2 per 120s window, 0.5s→5s backoff),
logs one event per relaunch, and is terminal-loud when the budget is spent.

Timer stubs replace only time: they have ``threading.Timer``'s exact shape
(delay, function, args) and the scheduler driving them is the real core
``GenerationScheduler``. Controller stubs keep the pinned protocol form from
``test_daemon_discovery.py``.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import sys
import time
from pathlib import Path
from typing import Any

from hyprial.kernel.impl.actor_runtime.policies import DEFAULT_POLICIES, PROCESS_LIFECYCLE
from hyprial.kernel.impl.actor_runtime.scheduler import GenerationScheduler
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.forwarding import (
    ForwardingControllerAuthority,
    ForwardingSidecarError,
    ForwardingSidecarSupervisor,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

FIXTURE = Path(__file__).resolve().parents[4] / 'tests/fixtures/network/fake_forwarding_sidecar.py'

#: The registered budget the supervisor runs under, asserted not redefined.
_POLICY = DEFAULT_POLICIES[PROCESS_LIFECYCLE]

#: The org-directory view the tests pin: two members, self excluded.
DIRECTORY = {
    "100.64.20.2": "tcPeerTwo",
    "100.64.20.3": "tcPeerThree",
}

#: The fixed slot set the backend dials (the only shape; no per-peer ports).
SLOTS = ("unixsock-stream/s/slot-00", "unixsock-stream/s/slot-01")


def _slots() -> tuple[str, ...]:
    return SLOTS


class _RecordedTimer:
    """``threading.Timer``'s shape; the test decides when it fires."""

    daemon = True

    def __init__(
        self,
        delay: float,
        function: Any,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> None:
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
        self.timers: list[_RecordedTimer] = []

    def __call__(
        self,
        delay: float,
        function: Any,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> _RecordedTimer:
        timer = _RecordedTimer(delay, function, args)
        self.timers.append(timer)
        return timer


class _Controller:
    """The pinned controller stub shape (see test_daemon_discovery.py)."""

    def __init__(self) -> None:
        self.mappings = {"100.64.20.2": 41002, "100.64.20.3": 41003}
        self.operations: list[tuple[str, str]] = []
        self.closed = False

    def status(self) -> tuple[tuple[str, ...], dict[str, int]]:
        return tuple(sorted(self.mappings)), dict(self.mappings)

    def map_peer(self, peer: str, address: str, slot: str) -> str:
        self.operations.append(("map", peer))
        raise AssertionError("stub is fully mapped; map_peer must not be called")

    def unmap_peer(self, peer: str) -> None:
        self.operations.append(("unmap", peer))
        self.mappings.pop(peer, None)

    def close(self) -> None:
        self.closed = True


class _FlakyFactory:
    """(environ, on_exit) -> controller; raises for the first N calls."""

    def __init__(self, failures: int) -> None:
        self.remaining = failures
        self.calls = 0
        self.controllers: list[_Controller] = []
        self.exit_callbacks: list[Any] = []

    def __call__(self, environ, on_exit) -> _Controller:
        self.calls += 1
        self.exit_callbacks.append(on_exit)
        if self.remaining > 0:
            self.remaining -= 1
            raise ForwardingSidecarError(f"boom {self.calls}")
        controller = _Controller()
        self.controllers.append(controller)
        return controller


def _supervisor(
    factory: Any, events: list[tuple[str, str, dict[str, Any]]]
) -> tuple[ForwardingSidecarSupervisor, _TimerRecorder]:
    recorder = _TimerRecorder()
    supervisor = ForwardingSidecarSupervisor(
        {},
        event_log=lambda level, event, **fields: events.append(
            (level, event, fields)
        ),
        controller_factory=factory,
        scheduler=GenerationScheduler(timer_factory=recorder),
        jitter_source=lambda: 0.5,  # jitter midpoint: nominal delays exactly
        slots=_slots,
    )
    return supervisor, recorder


def test_two_start_failures_then_success_recovers_endpoints() -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []
    factory = _FlakyFactory(failures=2)
    supervisor, recorder = _supervisor(factory, events)

    supervisor.ensure_started()
    assert recorder.timers[0].delay == 0.5  # PROCESS_LIFECYCLE base backoff
    recorder.timers[0].fire()
    assert recorder.timers[1].delay == 1.0  # exponential, still under the cap
    recorder.timers[1].fire()

    assert factory.calls == 3
    assert supervisor.state == "running"
    assert supervisor.endpoints() is not None
    assert supervisor.endpoints().list_reachable_endpoints() == SLOTS


    assert [(level, event) for level, event, _fields in events] == [
        ("error", "zenoh.forwarding.start_failed"),
        ("warn", "zenoh.forwarding.restarting"),
        ("error", "zenoh.forwarding.start_failed"),
        ("warn", "zenoh.forwarding.restarting"),
        ("info", "zenoh.forwarding.started"),
    ]
    restarting = [
        fields
        for _level, event, fields in events
        if event == "zenoh.forwarding.restarting"
    ]
    assert restarting == [
        {"reason": "start-failed", "failures": 1, "delaySeconds": 0.5},
        {"reason": "start-failed", "failures": 2, "delaySeconds": 1.0},
    ]
    started = [
        fields for _level, event, fields in events if event == "zenoh.forwarding.started"
    ]
    assert started == [
        {
            "failures": 2,
            "detail": "forwarding sidecar recovered after failure",
        }
    ]


def test_composed_supervisor_owns_native_controller_io() -> None:
    factory = _FlakyFactory(failures=0)
    supervisor = ForwardingSidecarSupervisor(
        {}, event_log=lambda *_args, **_fields: None,
        controller_factory=factory, own_io=True,
        slots=_slots,
    )
    supervisor.ensure_started()
    backend = supervisor.endpoints()
    assert backend is not None
    assert isinstance(backend.controller, ForwardingControllerAuthority)
    assert backend.list_reachable_endpoints() == SLOTS
    supervisor.close()
    assert factory.controllers[0].closed


def test_composed_supervisor_direct_peer_operations_reach_the_controller() -> None:
    """own_io=True must not strand map/unmap on the always-None ``_endpoints``.

    Regression: with the state owner holding the backend, the direct
    operation methods read ``self._endpoints`` -- None by construction in
    that mode -- and reported "forwarding sidecar is not running" on a
    healthy sidecar (E2E-025's org join hit exactly this).
    """

    class _Mappable(_Controller):
        def map_peer(self, peer: str, address: str, slot: str) -> str:
            self.operations.append(("map", peer))
            return slot

    controller = _Mappable()
    supervisor = ForwardingSidecarSupervisor(
        {}, event_log=lambda *_args, **_fields: None,
        controller_factory=lambda _environ, _on_exit: controller, own_io=True,
        slots=_slots,
    )
    supervisor.ensure_started()

    assert (
        supervisor.map_peer("peer-x", "tcp://10.0.0.9:7447#psk", "/s/slot-00")
        == "/s/slot-00"
    )
    supervisor.unmap_peer("peer-x")
    assert controller.operations == [("map", "peer-x"), ("unmap", "peer-x")]
    supervisor.close()


def test_composed_supervisor_fences_restart_decisions_in_state_actor() -> None:
    factory = _FlakyFactory(failures=1)
    recorder = _TimerRecorder()
    events: list[str] = []
    supervisor = ForwardingSidecarSupervisor(
        {}, event_log=lambda _level, event, **_fields: events.append(event),
        controller_factory=factory, own_io=True,
        scheduler=GenerationScheduler(timer_factory=recorder),
        jitter_source=lambda: 0.5,
    )
    supervisor.ensure_started()
    assert supervisor.state == "restarting"
    assert recorder.timers[0].delay == 0.5
    recorder.timers[0].fire()
    assert supervisor.state == "running"
    assert factory.calls == 2
    assert "zenoh.forwarding.started" in events
    supervisor.close()
    assert factory.controllers[0].closed


def test_persistent_start_failure_stops_at_the_registered_budget() -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []
    factory = _FlakyFactory(failures=99)
    supervisor, recorder = _supervisor(factory, events)

    supervisor.ensure_started()
    while recorder.timers and not recorder.timers[-1].fired:
        recorder.timers[-1].fire()

    # One initial attempt plus exactly max_restarts relaunches -- the budget
    # is the registered PROCESS_LIFECYCLE policy, not a number invented here.
    assert factory.calls == 1 + _POLICY.max_restarts
    assert len(recorder.timers) == _POLICY.max_restarts
    assert supervisor.state == "failed"
    assert supervisor.endpoints() is None
    terminal = [
        fields
        for _level, event, fields in events
        if event == "zenoh.forwarding.failed"
    ]
    assert len(terminal) == 1
    assert terminal[0]["failures"] == 1 + _POLICY.max_restarts
    assert terminal[0]["reason"] == "start-failed"
    assert "budget exhausted" in terminal[0]["detail"]


def test_requested_restart_replaces_the_closed_incarnation_immediately() -> None:
    """A deliberate reissue close has no exit callback to schedule recovery."""

    events: list[tuple[str, str, dict[str, Any]]] = []
    factory = _FlakyFactory(failures=0)
    supervisor, recorder = _supervisor(factory, events)

    supervisor.ensure_started()
    first = factory.controllers[0]

    supervisor.restart(reason="directory-conflict")

    assert first.closed is True
    assert factory.calls == 2
    assert factory.controllers[1].closed is False
    assert supervisor.state == "running"
    assert supervisor.endpoints() is not None
    assert recorder.timers == []


def test_requested_restart_failure_is_never_reported_running() -> None:
    """A failed replacement cannot leave the closed old backend installed."""

    events: list[tuple[str, str, dict[str, Any]]] = []
    factory = _FlakyFactory(failures=0)
    policy = type(_POLICY)(
        max_restarts=0,
        restart_window=_POLICY.restart_window,
        base_backoff=_POLICY.base_backoff,
        max_backoff=_POLICY.max_backoff,
        jitter_ratio=_POLICY.jitter_ratio,
    )
    supervisor = ForwardingSidecarSupervisor(
        {},
        event_log=lambda level, event, **fields: events.append(
            (level, event, fields)
        ),
        policy=policy,
        controller_factory=factory,
        own_io=True,
        slots=_slots,
    )
    supervisor.ensure_started()
    first = factory.controllers[0]
    factory.remaining = 1

    supervisor.restart(reason="directory-conflict")

    assert first.closed is True
    assert factory.calls == 2
    assert supervisor.state == "failed"
    assert supervisor.endpoints() is None


def test_exit_while_running_relaunches_within_the_backoff_bound() -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []
    factory = _FlakyFactory(failures=0)
    supervisor, recorder = _supervisor(factory, events)

    supervisor.ensure_started()
    assert supervisor.state == "running"
    first = factory.controllers[0]

    factory.exit_callbacks[0](17)

    assert first.closed is True  # the dead child is reaped, not leaked
    assert supervisor.state == "restarting"
    assert recorder.timers[0].delay == 0.5
    exited = [
        (level, fields)
        for level, event, fields in events
        if event == "zenoh.forwarding.exited"
    ]
    assert exited == [("error", {"exitStatus": 17})]
    restarting = [
        fields
        for _level, event, fields in events
        if event == "zenoh.forwarding.restarting"
    ]
    assert restarting == [
        {
            "reason": "exited",
            "failures": 1,
            "exitStatus": 17,
            "delaySeconds": 0.5,
        }
    ]

    recorder.timers[0].fire()
    assert factory.calls == 2
    assert supervisor.state == "running"
    assert supervisor.endpoints() is not None


def test_a_relaunch_recomputes_tcp_up_slots() -> None:
    from hyprial.kernel import FORWARDING_UP_ENV

    seen: list[list[str]] = []
    current = [["tcp:127.0.0.1:0", "tcp:127.0.0.1:0"]]

    def factory(environ, _on_exit):
        seen.append(json.loads(environ[FORWARDING_UP_ENV])["slots"])
        return _Controller()

    supervisor = ForwardingSidecarSupervisor(
        {
            FORWARDING_UP_ENV: json.dumps(
                {"v": 6, "op": "up", "slots": list(current[0])}
            )
        },
        event_log=lambda *_args, **_fields: None,
        controller_factory=factory,
        slots=lambda: (),
        slot_transport="tcp",
        up_slots=lambda: tuple(current[0]),
    )
    try:
        supervisor.ensure_started()
        current[0] = ["tcp:127.0.0.1:51230", "tcp:127.0.0.1:51231"]
        supervisor._spawn(expected_generation=1)
        assert seen == [
            ["tcp:127.0.0.1:0", "tcp:127.0.0.1:0"],
            ["tcp:127.0.0.1:51230", "tcp:127.0.0.1:51231"],
        ]
    finally:
        supervisor.close()


def test_close_cancels_pending_relaunch_and_is_terminal() -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []
    factory = _FlakyFactory(failures=99)
    supervisor, recorder = _supervisor(factory, events)

    supervisor.ensure_started()
    supervisor.close()

    assert recorder.timers[0].cancelled is True
    assert supervisor.state == "off"
    # A late firing must be a no-op, not a zombie relaunch.
    recorder.timers[0].fire()
    assert factory.calls == 1
    assert supervisor.endpoints() is None


def _app(tmp_path: Path, monkeypatch, *, command: str) -> DaemonApplication:
    monkeypatch.setenv("HYPRIAL_FORWARDING_COMMAND", command)
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
    # The test daemon never asks the host's tailscale for peers.
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    # A small fixed pool so the dialed slot set is short and assertable.
    monkeypatch.setenv("HYPRIAL_ZENOH_SLOT_POOL", "2")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return DaemonApplication(
        state_dir=tmp_path / "daemon-state",
        socket_path=tmp_path / "daemon-state/daemon.sock",
        node_id="relaunch-node",
        hyprial_home=tmp_path / "home",
        owner="tester",
    **_deep_module_daemon_dependencies())


def test_status_payload_exposes_off_running_and_failed(tmp_path, monkeypatch) -> None:
    plain = DaemonApplication(
        state_dir=tmp_path / "a-state",
        socket_path=tmp_path / "a-state/daemon.sock",
        node_id="status-node",
        hyprial_home=tmp_path / "a-home",
        owner="tester",
    **_deep_module_daemon_dependencies())
    try:
        assert plain._forwarding_status_json()["state"] == "off"
    finally:
        plain._close()

    app = _app(tmp_path, monkeypatch, command='["/missing/candidate","forward"]')
    try:
        from hyprial.daemon.impl.forwarding.slots import slot_endpoints

        # A failed start still dials the fixed slot set (derivable from the
        # home -- design §5), never an empty or fallback set.
        assert app._discover_peer_endpoints() == slot_endpoints(tmp_path / "home", 2)
        # Real timers under the real budget: 0.5s + 1.0s backoff, then terminal.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if app._forwarding_status_json()["state"] == "failed":
                break
            time.sleep(0.05)
        status = app._forwarding_status_json()
        assert status["state"] == "failed"
        assert status["failures"] == 1 + _POLICY.max_restarts
        assert status["pid"] is None
    finally:
        app._close()


def _one_shot_exit_wrapper(tmp_path: Path) -> Path:
    """First launch dies after ready (exit 17); later launches stay up."""

    wrapper = tmp_path / "one_shot_sidecar.py"
    wrapper.write_text(
        "import os, runpy\n"
        "from pathlib import Path\n"
        f"fixture = {str(FIXTURE)!r}\n"
        "marker = Path(os.environ['FWD_COEXIST_ONE_SHOT_MARKER'])\n"
        "if not marker.exists():\n"
        "    marker.write_text('1', encoding='utf-8')\n"
        "    os.environ['FAKE_FORWARD_EXIT_AFTER_READY'] = '17'\n"
        f"runpy.run_path(fixture, run_name='__main__')\n",
        encoding="utf-8",
    )
    return wrapper


def test_daemon_relaunches_real_sidecar_after_exit_and_recovers(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("FWD_COEXIST_ONE_SHOT_MARKER", str(tmp_path / "one-shot"))
    wrapper = _one_shot_exit_wrapper(tmp_path)
    app = _app(
        tmp_path, monkeypatch, command=json.dumps([sys.executable, str(wrapper)])
    )
    monkeypatch.setattr(app, "_tailcat_directory_peers", lambda: DIRECTORY)
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        app,
        "_log",
        lambda _level, _component, event, **fields: events.append((event, fields)),
    )
    try:
        from hyprial.daemon.impl.forwarding.slots import slot_endpoints

        slots = slot_endpoints(tmp_path / "home", 2)
        # The connect set is the fixed slot set across the exit and relaunch
        # -- it never changes, which is the whole point of the pool.
        assert app._discover_peer_endpoints() == slots
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if app._forwarding_status_json()["state"] == "running" and any(
                event == "zenoh.forwarding.started" for event, _fields in events
            ):
                break
            time.sleep(0.05)
        status = app._forwarding_status_json()
        assert status["state"] == "running"
        assert isinstance(status["pid"], int)
        assert app._discover_peer_endpoints() == slots
        names = [event for event, _fields in events]
        assert "zenoh.forwarding.exited" in names
        assert "zenoh.forwarding.restarting" in names
        assert "zenoh.forwarding.started" in names
    finally:
        app._close()


def test_composed_supervisor_passes_the_membership_bound_to_the_io_owner() -> None:
    """A request-path membership bound must reach the sidecar I/O owner intact."""

    class _Membership(_Controller):
        def membership_request(self, op: str, params: dict, *, timeout=None) -> dict:  # type: ignore[type-arg]
            self.operations.append(("membership", op))
            return {"readiness": {"sidecar": True, "control": True, "member": True}, "tailnets": []}

    controller = _Membership()
    supervisor = ForwardingSidecarSupervisor(
        {}, event_log=lambda *_args, **_fields: None,
        controller_factory=lambda _environ, _on_exit: controller, own_io=True,
        slots=_slots,
    )
    supervisor.ensure_started()
    try:
        result = supervisor.membership_request("membership-status", {}, timeout=10.0)
        assert result["tailnets"] == []
        assert controller.operations == [("membership", "membership-status")]
        # The bound is a caller choice: the default stays the background allowance.
        supervisor.membership_request("membership-status", {})
        assert len(controller.operations) == 2
    finally:
        supervisor.close()
