from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import pytest
import hyprial.daemon.impl.adapters.lark.worker.entry as lark_worker
from hyprial.daemon.impl.adapters.lark import HarnessDelivery, ReconcileReport
from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.daemon.impl.adapters.lark.worker.bridge import _serve_reply_bridge
from hyprial.daemon.impl.adapters.lark.worker.entry import _bounded_integer, _bounded_seconds, _rebuild_after_stale, _recovery_timing_from_environment
from hyprial.daemon.impl.adapters.lark.worker.sweep import STALE_REBUILD_EXIT_CODE
from hyprial.kernel import Logger
from hyprial.kernel import ChannelConfiguration, LarkGatewayConfig

from ..support import (
    _close_seam_health_monitors as _close_seam_health_monitors,
    _gateway,
    _launcher,
    _online_supervisor,
    _wait_for_reconcile_settlement,
    _worker_main_callback_seam,
    _write,
)


def test_worker_persists_reply_reaction_intent_before_control_receipt() -> None:
    daemon_end, worker_end = socket.socketpair()
    persisted = threading.Event()

    class Adapter:
        def handle_delivery(  # type: ignore[no-untyped-def]
            self, delivery: HarnessDelivery, *, settle: bool = True
        ):
            assert settle is False
            return SimpleNamespace(
                status="accepted", native_message_id="om-reply", error=None
            )

        def persist_delivery_reaction(self, reply_to: str) -> None:
            assert reply_to == "msg-lark-inbound"
            persisted.set()

        def clear_delivery_reaction(self, reply_to: str) -> None:
            assert reply_to == "msg-lark-inbound"

    worker_fd = worker_end.detach()
    thread = threading.Thread(
        target=_serve_reply_bridge,
        args=(Adapter(), worker_fd),  # type: ignore[arg-type]
    )
    thread.start()
    try:
        daemon_end.sendall(
            json.dumps(
                {
                    "deliveryId": "msg-reply",
                    "messageId": "msg-reply",
                    "replyTo": "msg-lark-inbound",
                    "fromActor": {
                        "actorId": "agent:allen:mac:pi",
                        "actorKey": "agent:allen:mac:pi",
                        "displayName": "allen/pi",
                    },
                    "text": "pong",
                },
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        daemon_end.settimeout(0.5)
        response = json.loads(daemon_end.recv(65_536).split(b"\n", 1)[0])
        assert persisted.is_set()
        assert response["ok"] is True
    finally:
        daemon_end.close()
        thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_running_worker_stream_staleness_is_visible_in_runtime_status(
    tmp_path: Path,
) -> None:
    script = _write(
        tmp_path,
        """
        import json, os, sys, time

        name = sys.argv[1]
        fd = int(os.environ["HYPRIAL_LARK_READY_FD"])
        def emit(value):
            os.write(fd, json.dumps(value).encode() + b"\\n")
        emit({"status": "online", "name": name})
        time.sleep(0.05)
        emit({
            "status": "health", "name": name, "streamHealth": "stale",
            "reason": "websocket-transport-stale",
            "connectedAt": "2026-08-10T00:00:00.000Z",
            "lastEventAt": None,
            "lastTransportAt": "2026-08-10T00:00:00.000Z",
            "lastProbeAt": "2026-08-10T00:15:01.000Z",
        })
        while True:
            time.sleep(0.05)
        """,
    )
    launcher = _launcher(tmp_path, script)
    supervisor = AdapterRuntime(
        launcher,
        ChannelConfiguration(gateways=(_gateway(),)),
    )
    try:
        assert supervisor.start("lark-test") is True
        deadline = time.monotonic() + 3.0
        status: dict[str, object] = {}
        while time.monotonic() < deadline:
            status = supervisor.status("lark-test")[0]
            if status.get("status") == "stale":
                break
            time.sleep(0.02)

        assert status["status"] == "stale"
        assert status["online"] is False
        assert status["processRunning"] is True
        assert status["streamHealth"] == "stale"
        assert status["knownChatsRecovery"] == "supported"
        assert status["unknownFirstChatRecovery"] == "unsupported"
        assert status["chatEnumeration"] == "not-implemented"
        assert status["lastEventAt"] is None
        assert status["lastProbeAt"] == "2026-08-10T00:15:01.000Z"
        events = supervisor.drain_health_events()
        assert events[-1]["event"] == "lark.inbound.stale"
        assert events[-1]["reason"] == "websocket-transport-stale"
    finally:
        supervisor.stop()


def test_running_worker_pin_query_failure_reaches_daemon_event_stream(
    tmp_path: Path,
) -> None:
    script = _write(
        tmp_path,
        """
        import json, os, sys, time

        name = sys.argv[1]
        fd = int(os.environ["HYPRIAL_LARK_READY_FD"])
        def emit(value):
            os.write(fd, json.dumps(value).encode() + b"\\n")
        emit({"status": "online", "name": name})
        emit({
            "status": "warning",
            "name": name,
            "event": "lark.pin.query_failed",
            "errorType": "ConnectionRefusedError",
            "fallback": "last-successful-pin",
            "cachedActor": "agent:alice:mac:cached",
        })
        while True:
            time.sleep(0.05)
        """,
    )
    supervisor = AdapterRuntime(
        _launcher(tmp_path, script),
        ChannelConfiguration(gateways=(_gateway(),)),
    )
    try:
        assert supervisor.start("lark-test") is True
        deadline = time.monotonic() + 3.0
        events: list[dict[str, object]] = []
        while time.monotonic() < deadline and not events:
            events.extend(supervisor.drain_health_events())
            time.sleep(0.02)

        assert events == [
            {
                "event": "lark.pin.query_failed",
                "adapter": "lark-test",
                "errorType": "ConnectionRefusedError",
                "fallback": "last-successful-pin",
                "cachedActor": "agent:alice:mac:cached",
            }
        ]
    finally:
        supervisor.stop()


def test_running_worker_history_check_is_explicit_and_not_online(
    tmp_path: Path,
) -> None:
    script = _write(
        tmp_path,
        """
        import json, os, sys, time

        name = sys.argv[1]
        fd = int(os.environ["HYPRIAL_LARK_READY_FD"])
        os.write(fd, json.dumps({"status": "online", "name": name}).encode() + b"\\n")
        os.write(fd, json.dumps({
            "status": "health", "name": name, "streamHealth": "checking",
            "reason": "history-reconcile-started"
        }).encode() + b"\\n")
        while True:
            time.sleep(0.05)
        """,
    )
    supervisor = AdapterRuntime(
        _launcher(tmp_path, script),
        ChannelConfiguration(gateways=(_gateway(),)),
    )
    try:
        assert supervisor.start("lark-test") is True
        deadline = time.monotonic() + 3.0
        status: dict[str, object] = {}
        while time.monotonic() < deadline:
            status = supervisor.status("lark-test")[0]
            if status.get("status") == "checking":
                break
            time.sleep(0.02)

        assert status["status"] == "checking"
        assert status["online"] is False
        assert status["streamHealth"] == "checking"
        assert status["reason"] == "history-reconcile-started"
    finally:
        supervisor.stop()


@pytest.mark.parametrize(
    "raw",
    ["nan", "inf", "-inf", "1e309", "0", "-1", "1e-12", "1e99", "invalid"],
)
def test_lark_timing_env_rejects_non_finite_and_unsafe_values(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("HYPRIAL_TEST_TIMING_SECONDS", raw)

    assert _bounded_seconds(
        "HYPRIAL_TEST_TIMING_SECONDS", 30.0, minimum=1.0, maximum=3600.0
    ) == 30.0


@pytest.mark.parametrize(
    ("name", "index", "default"),
    [
        ("HYPRIAL_LARK_STALE_AFTER_SECONDS", 0, 900.0),
        ("HYPRIAL_LARK_HEALTH_CHECK_INTERVAL_SECONDS", 1, 30.0),
        ("HYPRIAL_LARK_REST_PROBE_TIMEOUT_SECONDS", 2, 10.0),
        ("HYPRIAL_LARK_IDLE_RECONCILE_TIMEOUT_SECONDS", 3, 60.0),
    ],
)
def test_each_lark_timing_env_is_wired_to_fail_safe_bounds(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    index: int,
    default: float,
) -> None:
    monkeypatch.setenv(name, "1e309")

    assert _recovery_timing_from_environment()[index] == default


@pytest.mark.parametrize("raw", ["", "1.5", "0", "-1", "501", "invalid"])
def test_dead_letter_threshold_rejects_unsafe_values(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("HYPRIAL_LARK_DEAD_LETTER_ALERT_THRESHOLD", raw)

    assert _bounded_integer(
        "HYPRIAL_LARK_DEAD_LETTER_ALERT_THRESHOLD",
        10,
        minimum=1,
        maximum=500,
    ) == 10


def test_dead_letter_threshold_accepts_bounded_integer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPRIAL_LARK_DEAD_LETTER_ALERT_THRESHOLD", "25")

    assert _bounded_integer(
        "HYPRIAL_LARK_DEAD_LETTER_ALERT_THRESHOLD",
        10,
        minimum=1,
        maximum=500,
    ) == 25


def test_worker_stale_rebuild_uses_documented_numeric_exit_code() -> None:
    exits: list[int] = []

    _rebuild_after_stale(
        "history-probe-failed",
        pause=lambda _seconds: None,
        exit_process=exits.append,
    )

    assert STALE_REBUILD_EXIT_CODE == 75
    assert exits == [75]


def test_exit_75_worker_is_restarted_with_supervisor_backoff(tmp_path: Path) -> None:
    spawn_log = tmp_path / "spawns.log"
    script = _write(
        tmp_path,
        """
        import json, os, sys, time

        name = sys.argv[1]
        with open(os.environ["SPAWN_LOG"], "a", encoding="utf-8") as stream:
            stream.write("spawn\\n")
        fd = int(os.environ["HYPRIAL_LARK_READY_FD"])
        os.write(fd, json.dumps({"status": "online", "name": name}).encode() + b"\\n")
        os.write(fd, json.dumps({
            "status": "health", "name": name, "streamHealth": "stale",
            "reason": "history-probe-failed"
        }).encode() + b"\\n")
        time.sleep(0.03)
        raise SystemExit(75)
        """,
    )
    launcher = _launcher(tmp_path, script)
    launcher.env["SPAWN_LOG"] = str(spawn_log)
    # The back-off is DRIVEN, ⛔ not waited for.  This test needs three
    # different answers to "has the retry delay elapsed" in one run -- yes,
    # then no, then yes again -- so with a real clock it is asserting how fast
    # this machine is, and no value of `retry_interval` satisfies all three
    # (enlarging it steadies the "no" and breaks both "yes"es).  Under a
    # throttled runner it collects: CI tasks 15613, 15991, 16277, 16686.
    now = [1_000.0]
    supervisor = AdapterRuntime(
        launcher,
        ChannelConfiguration(gateways=(_gateway(),)),
        retry_interval=0.25,
        clock=lambda: now[0],
    )
    try:
        assert supervisor.start("lark-test") is True
        first = supervisor._processes["lark-test"]
        first._process.wait(timeout=2)
        assert first._process.returncode == 75

        assert supervisor.reconcile() == 1
        second = supervisor._processes["lark-test"]
        second._process.wait(timeout=2)
        assert second._process.returncode == 75

        # Still inside the back-off window: the clock has not moved.
        assert supervisor.reconcile() == 0
        assert spawn_log.read_text(encoding="utf-8").splitlines() == [
            "spawn",
            "spawn",
        ]

        now[0] += 0.3  # past retry_interval=0.25
        assert supervisor.reconcile() == 1
    finally:
        supervisor.stop()


def test_worker_main_wires_dead_letter_alerts_to_the_operator_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HYPRIAL_LARK_DEAD_LETTER_ALERT_THRESHOLD", "25")
    operator_route = SimpleNamespace(
        name="operators",
        type="direct",
        native_id="oc_operator",
        members=(),
    )
    seam = _worker_main_callback_seam(
        monkeypatch,
        tmp_path,
        lambda _call, _lookback: ReconcileReport(0, 0, 0, 0, 0),
        gateway_routes=(operator_route,),
        default_route="operators",
    )

    assert isinstance(seam.adapter_kwargs["logger"], Logger)
    assert seam.adapter_kwargs["dead_letter_alert_threshold"] == 25
    notify_operator = seam.adapter_kwargs["notify_operator"]
    assert notify_operator(
        "Dead-letter backlog: count=25; reasons=no-target:25",
        "dead-letter-backlog:lark-test:1786870800",
    )
    assert seam.notifications == [
        (
            "oc_operator",
            "Dead-letter backlog: count=25; reasons=no-target:25",
            "dead-letter-backlog:lark-test:1786870800:operators",
        )
    ]


def test_worker_main_mints_the_current_adapter_lark_spelling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PAC NAMB efeeaf0a01ba: every new channel-actor mint uses adapter:lark:,
    never the legacy channel:lark: spelling — historical channel:lark:
    identities already on the wire keep parsing (dual-read), but nothing
    should mint that spelling anymore."""

    seam = _worker_main_callback_seam(
        monkeypatch,
        tmp_path,
        lambda _call, _lookback: ReconcileReport(0, 0, 0, 0, 0),
    )

    assert seam.adapter_kwargs["channel_actor_id"] == "adapter:lark:lark-test"


def test_worker_main_identity_lookup_failure_leaves_a_telemetry_trace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing identities lookup degrades to the raw id, but observably.

    Field report 2026-09-14: an identities schema mismatch made every
    inbound lookup fail, and the ``except Exception`` fallback swallowed
    it -- display names silently stopped resolving.  The fallback
    stays (lookup must never break inbound), but the failure must land on
    the worker telemetry stream, same practice as ``lark.pin.query_failed``.
    """

    gateway = SimpleNamespace(
        name="lark-test",
        app_id="cli_test_app",
        credential_ref="lark-test",
        routes=(),
        default_route=None,
    )

    class Store:
        def load(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                channels=SimpleNamespace(gateways=(gateway,))
            )

        def lark_app_secret(self, credential_ref: str) -> str:
            return "unit-test-secret"

    class BrokenState:
        """The store as an unmigrated database presents it: every read dies."""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def find_identities(self, **_kwargs: object) -> tuple:
            raise RuntimeError("no such column: hyprial_owner")

        def close(self) -> None:
            pass

    class Adapter:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def begin_backfill(self) -> None:
            pass

        def reconcile_recent(self, *, lookback_seconds: int):  # type: ignore[no-untyped-def]
            return ReconcileReport(0, 0, 0, 0, 0)

        def handle_sdk_event(self, _event: object) -> None:
            pass

        def handle_member_change_event(self, _event: object) -> None:
            pass

    resolvers: list[Any] = []

    class Gateway:
        app_id = "cli_test_app"
        app_secret = "unit-test-secret"

        def set_sender_resolver(self, resolver: Any) -> None:
            resolvers.append(resolver)

    class Stream:
        def __init__(self, **callbacks: Any) -> None:
            pass

        def probe_rest_endpoint(self) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("HYPRIAL_LARK_READY_FD", raising=False)
    monkeypatch.setattr(
        lark_worker, "PersistentConfigStore", lambda *_args: Store()
    )
    monkeypatch.setattr(
        lark_worker.LarkSdkGateway,
        "from_credentials",
        lambda *_args, **_kwargs: Gateway(),
    )
    monkeypatch.setattr(lark_worker, "LarkAdapter", lambda **_kwargs: Adapter())
    monkeypatch.setattr(lark_worker, "LarkEventStream", Stream)
    monkeypatch.setattr(lark_worker, "LarkStateStore", BrokenState)
    monkeypatch.setattr(
        lark_worker,
        "_recovery_timing_from_environment",
        lambda: (900.0, 3600.0, 0.02, 0.02),
    )

    assert lark_worker.main(["lark-test"]) == 0
    assert len(resolvers) == 1

    capsys.readouterr()  # drop startup telemetry (online/health lines)
    # Inbound semantics unchanged: the resolver degrades to the raw id.
    assert resolvers[0]("ou_someone") is None
    events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    traces = [
        event
        for event in events
        if event.get("event") == "lark.identity.lookup_failed"
    ]
    assert len(traces) == 1
    assert traces[0]["status"] == "warning"
    assert traces[0]["name"] == "lark-test"
    assert traces[0]["errorType"] == "RuntimeError"
    assert "no such column: hyprial_owner" in traces[0]["error"]
    assert traces[0]["fallback"] == "raw-platform-id"


def test_worker_main_keeps_live_stream_when_history_scope_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """History recovery is optional for an app restricted to @-mentions."""

    seam = _worker_main_callback_seam(
        monkeypatch,
        tmp_path,
        lambda _call, _lookback: ReconcileReport(
            1, 0, 0, 0, 0, retryable_errors=("oc_demo: history-permission-unavailable",)
        ),
    )

    seam.stream.callbacks["on_ready"]()
    _wait_for_reconcile_settlement(seam.health)

    assert seam.rebuilds == []
    assert seam.health.rebuild_latched() is False
    assert seam.health.connected() is True


def test_worker_main_ready_runs_one_reconcile_then_becomes_healthy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seam = _worker_main_callback_seam(
        monkeypatch,
        tmp_path,
        lambda _call, _lookback: ReconcileReport(1, 0, 0, 0, 0),
    )

    seam.stream.callbacks["on_ready"]()
    deadline = time.monotonic() + 1.0
    while seam.adapter.calls < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    _wait_for_reconcile_settlement(seam.health)
    assert seam.health.drain_telemetry()
    health = [
        value
        for line in capsys.readouterr().out.splitlines()
        if (value := json.loads(line)).get("status") == "health"
    ]

    assert seam.adapter.calls == 1
    assert health[0]["streamHealth"] == "checking"
    assert health[-1]["streamHealth"] == "healthy"


def test_reload_gateways_adds_new_adapter_without_touching_running_workers(
    tmp_path: Path,
) -> None:
    supervisor = _online_supervisor(tmp_path)
    try:
        before = supervisor._processes["lark-test"]
        assert before.running

        summary = supervisor.reload_gateways(
            ChannelConfiguration(
                gateways=(
                    _gateway(),
                    LarkGatewayConfig(
                        name="lark-new",
                        app_id="cli_unit_test_new",
                        credential_ref="lark-test",
                        routes=(),
                    ),
                )
            )
        )

        assert summary == {
            "added": ["lark-new"],
            "updated": [],
            "removed": [],
            "removedRunning": [],
        }
        # The running worker is the same live process, not a respawn.
        assert supervisor._processes["lark-test"] is before
        assert before.running
        # The freshly reloaded adapter is startable without a daemon restart.
        assert supervisor.start("lark-new") is True
        assert supervisor._processes["lark-new"].running
    finally:
        supervisor.stop()


def test_reload_gateways_removes_config_but_keeps_the_worker_running(
    tmp_path: Path,
) -> None:
    supervisor = _online_supervisor(tmp_path)
    try:
        summary = supervisor.reload_gateways(ChannelConfiguration(gateways=()))

        assert summary["removed"] == ["lark-test"]
        assert summary["removedRunning"] == ["lark-test"]
        assert supervisor._processes["lark-test"].running
        # No longer configured: it must not be revived as desired state.
        assert "lark-test" not in supervisor._desired
        assert supervisor.gateway_names == ()
    finally:
        supervisor.stop()
    assert not supervisor._processes


def test_removed_but_running_worker_stays_visible_in_status(
    tmp_path: Path,
) -> None:
    """No ghost window: a worker outliving its config must stay reportable.

    Mutation anchor: enumerating only ``_gateways`` in ``status()`` drops the
    running worker from every view and this test goes red.
    """

    supervisor = _online_supervisor(tmp_path)
    try:
        supervisor.reload_gateways(ChannelConfiguration(gateways=()))

        (ghost,) = supervisor.status()
        assert ghost["name"] == "lark-test"
        assert ghost["configured"] is False
        assert ghost["processRunning"] is True
        assert ghost["status"] == "detached"
        assert ghost["online"] is False

        # A name query finds it too -- it must not read as NOT_FOUND.
        (by_name,) = supervisor.status("lark-test")
        assert by_name["status"] == "detached"
    finally:
        supervisor.stop()
    # Once the worker is gone, the name disappears entirely.
    assert supervisor.status() == ()


def test_reload_gateways_reports_changed_entries(tmp_path: Path) -> None:
    supervisor = _online_supervisor(tmp_path)
    try:
        summary = supervisor.reload_gateways(
            ChannelConfiguration(
                gateways=(
                    LarkGatewayConfig(
                        name="lark-test",
                        app_id="cli_unit_test_rotated",
                        credential_ref="lark-test",
                        routes=(),
                    ),
                )
            )
        )

        assert summary["updated"] == ["lark-test"]
        # A changed entry applies at the next explicit start, not now.
        assert supervisor._processes["lark-test"].running
    finally:
        supervisor.stop()


def test_worker_main_stops_its_health_watcher_when_main_returns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A returned main() must not leave a health thread that can later exit the process."""

    _worker_main_callback_seam(
        monkeypatch,
        tmp_path,
        lambda _call, _lookback: ReconcileReport(1, 0, 0, 0, 0),
    )

    watchers = [
        thread for thread in threading.enumerate()
        if thread.name == "lark-health-lark-test"
    ]
    for thread in watchers:
        thread.join(timeout=2.0)
    assert [thread for thread in watchers if thread.is_alive()] == []
