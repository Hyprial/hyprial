"""Native startup checkpoints, one graph definition and conservative routine probes."""
from __future__ import annotations

import ast
from dataclasses import fields, is_dataclass
import inspect
import json
import socket

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.wiring import graph as graph_module
from hyprial.daemon.impl.transport import ZenohConfig
from hyprial.kernel import DaemonRequestError, DaemonStartupPhase, ipc_errors
from hyprial.shell.impl.daemon_wiring import daemon_dependencies


@pytest.fixture
def application(tmp_path, own_daemon_application):
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    return own_daemon_application(DaemonApplication(
        state_dir=state, socket_path=state / "daemon.sock",
        node_id="wiring-ownership", owner="wiring-owner", hyprial_home=home,
        **daemon_dependencies(),
    ))


def _start_native_runtime(application, monkeypatch):
    """Execute the real graph with a real home lease and guarded native transport."""
    application.network_isolated = True
    application.zenoh_connect = ()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        application.zenoh_listen = (
            f"tcp/127.0.0.1:{reservation.getsockname()[1]}",
        )
    native_build = ZenohConfig.build

    def guarded_build(config):
        assert not config.gossip_scouting
        assert not config.multicast_scouting
        assert config.listen
        for endpoint in (*config.listen, *config.connect):
            host = endpoint.partition("/")[2].rsplit(":", 1)[0].strip("[]")
            assert host in {"127.0.0.1", "::1", "localhost"}
        return native_build(config)

    monkeypatch.setattr(ZenohConfig, "build", guarded_build)
    application._home_guard.claim()
    application._migrate_logs_at_startup()
    application._startup_step(
        application._start_runtime, DaemonStartupPhase.ACTOR_RUNTIME,
    )


def _closed_records(application):
    application._close()
    return [
        json.loads(line)
        for line in (application.state_dir / "logs" / "daemon.jsonl").read_text().splitlines()
    ]


def test_native_startup_keeps_actor_checkpoint_order_without_late_forwarding(application, monkeypatch):
    _start_native_runtime(application, monkeypatch)
    records = _closed_records(application)
    expected = [
        DaemonStartupPhase.ACTOR_RUNTIME,
        DaemonStartupPhase.ACTOR_RUNTIME_AGENTS_STORE,
        DaemonStartupPhase.ACTOR_RUNTIME_TRANSPORT,
        DaemonStartupPhase.ACTOR_RUNTIME_ORGFS_TRANSPORT,
        DaemonStartupPhase.ACTOR_RUNTIME_IDENTITY_BINDINGS,
        DaemonStartupPhase.ACTOR_RUNTIME_CASDOOR_SYNC,
        DaemonStartupPhase.ACTOR_RUNTIME_INBOX_STORE,
        DaemonStartupPhase.ACTOR_RUNTIME_LIFECYCLE_STORE,
    ]
    begins = [row["phase"] for row in records if row.get("event") == "daemon.start.begin"]
    ends = [row["phase"] for row in records if row.get("event") == "daemon.start.end"]

    # Isolated startup has no forwarding work. Non-isolated forwarding has its
    # own early phase before Zenoh; it is never a later actor-runtime no-op.
    assert DaemonStartupPhase.ACTOR_RUNTIME_FORWARDING_RECONFIGURE not in begins
    assert begins == expected
    assert ends == [*expected[1:], expected[0]]
    assert not [row for row in records if row.get("event") == "daemon.start.failed"]


def test_startup_graph_has_one_canonical_definition():
    tree = ast.parse(inspect.getsource(graph_module))
    definitions = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_StartupGraph"
    ]

    assert len(definitions) == 1
    assert [ast.unparse(item) for item in definitions[0].decorator_list] == ["dataclass"]


def test_startup_graph_preserves_fields_and_mutable_startup_handles():
    graph_type = graph_module._StartupGraph
    required = (
        "transport", "forwarding_listen", "directory", "presence", "local_delivery",
        "delivery", "lark_events", "adapters", "lark_client", "inbox_coordinator",
        "inbox", "shared_inbox_events", "hold_policy", "delivery_hooks",
        "harnesses",
    )
    optional = (
        "endpoint", "status_endpoint", "user_endpoint", "user_delivery", "org_endpoint",
        "recipient_wakes", "stop_recipient_wake_observer", "duplicate_watch",
        "actor_token", "runtime", "recovery", "in_channel_notice_delivery",
    )
    handles = {name: object() for name in required}
    graph = graph_type(**handles)
    runtime = object()
    graph.runtime = runtime

    assert is_dataclass(graph_type)
    assert tuple(field.name for field in fields(graph_type)) == required + optional
    assert graph.runtime is runtime
    assert graph.transport is handles["transport"]
    assert all(getattr(graph, name) is None for name in optional if name != "runtime")
    assert not graph_type.__dataclass_params__.frozen


def test_live_bridge_harnesses_expose_the_steer_port(application, monkeypatch):
    """The real startup graph hands the bridge the steer-capable port.

    The T1 timeout path folds a queued delivery in through
    ``DaemonEventBridge.harnesses`` (``getattr(self.harnesses, "steer", None)``).
    If the graph's port lacks ``steer``, every T1 hit degrades to the fail-loud
    notice instead of steering.  This pins the object the live wiring actually
    passes, and that it is the same one the lifecycle manager owns.
    """
    from hyprial.daemon.impl.composition import HarnessPortClient

    _start_native_runtime(application, monkeypatch)
    try:
        harnesses = application._runtime.harnesses
        assert isinstance(harnesses, HarnessPortClient)
        assert callable(getattr(harnesses, "steer", None))
        assert harnesses is application._harnesses
        # Live actor answer, not a stub: an unknown harness is an explicit
        # not-accepted, so the bridge reads a failed steer and notices.
        assert harnesses.steer("no-such-harness", "no-such-delivery") is False
    finally:
        application._close()


def test_prestart_routine_probe_declares_unavailable(application):
    with pytest.raises(DaemonRequestError, match="routine runtime is not running") as failure:
        application._assign_routine_probe("missing")

    assert failure.value.code == ipc_errors.ROUTINE_UNAVAILABLE


@pytest.mark.parametrize("unopenable_database,event", [
    ("workflows.sqlite3", "workflow.cutover_failed"),
    ("routines.sqlite3", "routine.recovery_unavailable"),
])
def test_native_degraded_startup_probe_declares_unavailable(
    application, monkeypatch, unopenable_database, event,
):
    # Real SQLite failure, not a fake startup method or replacement factory.
    (application.state_dir / unopenable_database).mkdir()
    _start_native_runtime(application, monkeypatch)

    assert application._routine_service is None
    with pytest.raises(DaemonRequestError, match="routine runtime is not running") as failure:
        application._assign_routine_probe("missing")
    assert failure.value.code == ipc_errors.ROUTINE_UNAVAILABLE
    records = _closed_records(application)
    assert any(row.get("event") == event for row in records)
    assert any(row.get("event") == "service.recovery.completed" for row in records)


@pytest.mark.parametrize("present", [False, True])
def test_real_routine_probe_preserves_presence_facts(application, monkeypatch, present):
    _start_native_runtime(application, monkeypatch)
    assert application._routine_service is not None
    name = "wiring-presence-probe"
    if present:
        text = application._routine_template_renderer(
            "agent-home-setup", name=name,
            owner="agent:wiring-owner:wiring-ownership:resident",
            escalate_to="user:wiring-owner",
        )
        application._routine_service.add(
            yaml_text=text, owner="user:wiring-owner", enabled=False,
        )

    assert application._assign_routine_probe(name) is present


def test_real_routine_probe_read_failure_remains_unknown(application, monkeypatch):
    from hyprial.biz import RoutineStore

    _start_native_runtime(application, monkeypatch)
    error = OSError("synthetic routine read failure")

    def fail_read(store, name):
        raise error

    monkeypatch.setattr(RoutineStore, "get_routine", fail_read)
    with pytest.raises(OSError, match="synthetic routine read failure") as failure:
        application._assign_routine_probe("missing")

    assert failure.value is error
