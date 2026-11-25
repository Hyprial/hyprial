"""Workflow node context crosses the daemon request boundary exactly once."""

import json
import sqlite3
from pathlib import Path

import pytest

from hyprial.daemon.impl.pac.views.context import node_context
from hyprial.kernel import RequestPortError, daemon_request, ipc_errors
from tests.daemon.ipc.contracts.agent.test_relations_specs import app as app
from tests.daemon.workers.test_worker_relay import isolated as isolated
from tests.support.pac.builders import build_allen_graph

from typer.testing import CliRunner

from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY, MIGRATED_METHODS
from hyprial.shell.impl.cli.commands.common import support as cli_support
from hyprial.shell.impl.cli.commands.common.root import app as cli_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401


def test_context_is_registered_as_private_safe_ipc():
    binding = METHOD_REGISTRY.get("workflow.node.context")
    assert binding is not None
    assert "workflow.node.context" in MIGRATED_METHODS
    assert (binding.spec.mutates, binding.spec.retry, binding.spec.http_exposed) == (False, "safe", False)


def test_context_cli_sends_one_request_and_keeps_document(monkeypatch):
    import hyprial.daemon as daemon

    calls = []
    expected = {"ok": True, "graphId": "g-1", "nodeId": "n-1", "version": 3,
                "cursor": 7, "briefRef": "brief:x", "completedCount": 2,
                "currentActivation": {"round": 1}}
    monkeypatch.setattr(daemon, "node_context", lambda *a, **k: (_ for _ in ()).throw(AssertionError("local read")))
    monkeypatch.setattr(cli_support, "_daemon_request", lambda method, params=None, **kw:
                        calls.append((method, params)) or expected)
    result = CliRunner().invoke(cli_app, ["workflow", "inspect", "g-1", "--node", "n-1", "--context", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == expected
    assert calls == [("workflow.node.context", {"runId": "g-1", "target": "n-1"})]


@pytest.fixture()
def workflow_socket(plan_socket, tmp_path):
    plan_socket.state_dir = tmp_path
    plan_socket.owner = "allen"
    plan_socket.node_id = "test-node"
    plan_socket._workflow_service = None
    store, graph_id = build_allen_graph(tmp_path)
    try:
        yield plan_socket, store, graph_id
    finally:
        store.close()


def test_real_socket_cli_context_matches_old_snapshot(workflow_socket, monkeypatch):
    import hyprial.daemon.impl.pac.views.context as context_module

    app, store, graph_id = workflow_socket
    poison = app.state_dir / "notes" / "allen-example.md"
    poison.parent.mkdir()
    poison.write_text("reference body must never be read", encoding="utf-8")
    open_path = Path.open

    def guard_open(path, *args, **kwargs):
        assert path != poison, "context dereferenced a reference body"
        return open_path(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guard_open)
    calls = []
    original = context_module.snapshot

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(context_module, "snapshot", counted)
    monkeypatch.setattr(context_module, "_local_identity", lambda: ("allen", "test-node"))
    expected = node_context(store.path, graph_id, "pm-helper")
    calls.clear()
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "agent:foreign:elsewhere:intruder")
    monkeypatch.setenv("HARNESS_STATE_DIR", "/not-the-daemon-state")
    monkeypatch.setattr(cli_support, "_daemon_request", lambda method, params=None, **kw:
        daemon_request(method, params, socket_path=app.socket_path, timeout=10.0))
    runner = CliRunner()
    result = runner.invoke(cli_app, ["workflow", "inspect", graph_id, "--node", "pm-helper", "--context", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == expected
    assert calls == [(store.path, graph_id)]
    calls.clear()
    text = runner.invoke(cli_app, ["workflow", "inspect", graph_id, "--node", "pm-helper", "--context"])
    assert text.exit_code == 0, text.output
    assert f"graph {graph_id} node pm-helper version {expected['version']} cursor {expected['cursor']}" in text.stdout
    assert "currentActivation: none" in text.stdout
    assert calls == [(store.path, graph_id)]


def test_socket_reports_missing_graph_node_empty_and_resync(workflow_socket):
    app, store, graph_id = workflow_socket
    for params, code in [({"runId": "", "target": ""}, "PAC_GRAPH_NOT_FOUND"),
                         ({"runId": graph_id, "target": ""}, "PAC_NODE_NOT_FOUND"),
                         ({"runId": graph_id, "target": "absent"}, "PAC_NODE_NOT_FOUND")]:
        with pytest.raises(RequestPortError) as error:
            daemon_request("workflow.node.context", params, socket_path=app.socket_path, timeout=10.0)
        assert error.value.code == code
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA user_version=0")
    with pytest.raises(RequestPortError) as error:
        daemon_request("workflow.node.context", {"runId": graph_id, "target": "pm-helper"},
                       socket_path=app.socket_path, timeout=10.0)
    assert error.value.code == "PAC_EVENTS_RESYNC"
    assert error.value.data == {"resync": True}


def test_corrupt_database_is_standard_daemon_error(plan_socket, tmp_path):
    plan_socket.state_dir = tmp_path
    (tmp_path / "pac-graph.sqlite3").write_bytes(b"not a sqlite database")
    with pytest.raises(RequestPortError) as error:
        daemon_request("workflow.node.context", {"runId": "g", "target": "n"},
                       socket_path=plan_socket.socket_path, timeout=10.0)
    assert error.value.code == "DAEMON_ERROR"
    assert "not a database" in str(error.value)


def test_worker_relay_does_not_expose_context(isolated):
    from tests.daemon.workers.test_worker_relay import frame, request

    _, _, relay, _ = isolated
    result = request(relay.socket_path, frame("workflow.node.context", {"runId": "g", "target": "n"}))
    assert result["error"]["code"] == "METHOD_NOT_FOUND"


def test_actor_association_uses_daemon_identity_not_client_environment(plan_socket, tmp_path, monkeypatch):
    from hyprial.daemon.impl.pac.graphs.edits import add_node, create_graph
    from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path

    plan_socket.state_dir = tmp_path
    plan_socket.owner = "allen"
    plan_socket.node_id = "test-node"
    store = PacGraphStore(default_database_path(tmp_path))
    try:
        graph_id = create_graph(store, name="binding", created_by="user:allen")["graphId"]
        version = add_node(store, tmp_path, graph_id=graph_id, node_id="actor", owner="user:allen",
            brief_ref="brief:actor", kind="actor", actor_name="w", launch_ref="launch/w.yaml", expect_version=1)["version"]
        add_node(store, tmp_path, graph_id=graph_id, node_id="task", owner="agent:allen:test-node:w",
            brief_ref="brief:task", expect_version=version)
        expected = node_context(store.path, graph_id, "task", local_identity=("allen", "test-node"))
        foreign = node_context(store.path, graph_id, "task", local_identity=("other", "elsewhere"))
        assert expected["actor"]["actorName"] == "w"
        assert "actor" not in foreign
        monkeypatch.setenv("HYPRIAL_OWNER", "other")
        monkeypatch.setenv("HYPRIAL_NODE_ID", "elsewhere")
        result = daemon_request("workflow.node.context", {"runId": graph_id, "target": "task"},
                                socket_path=plan_socket.socket_path, timeout=10.0)
        assert result == expected
    finally:
        store.close()


def test_untrusted_identity_or_path_parameters_are_not_accepted(plan_socket):
    for key in ("owner", "actor", "sessionRef", "stateDir"):
        with pytest.raises(RequestPortError) as error:
            daemon_request("workflow.node.context", {"runId": "g", "target": "n", key: "forged"},
                           socket_path=plan_socket.socket_path, timeout=10.0)
        assert error.value.code == "INVALID_ARGUMENT"


def test_restore_rejects_before_read(plan_socket, monkeypatch):
    import hyprial.daemon.impl.pac.views.context as context_module

    monkeypatch.setattr(context_module, "snapshot", lambda *args: pytest.fail("read during restore"))
    plan_socket._restore_done.clear()
    try:
        with pytest.raises(ipc_errors.DaemonRestoringError) as error:
            daemon_request("workflow.node.context", {"runId": "g", "target": "n"},
                           socket_path=plan_socket.socket_path, timeout=10.0, restore_wait=0)
        assert error.value.code == "DAEMON_RESTORING"
    finally:
        plan_socket._restore_done.set()


def test_offline_cli_reports_daemon_unavailable_without_local_fallback(workflow_socket, monkeypatch):
    app, store, graph_id = workflow_socket
    calls = []
    monkeypatch.setattr(cli_support, "_daemon_request", lambda method, params=None, **kw:
        calls.append((method, params)) or (_ for _ in ()).throw(ipc_errors.DaemonUnavailableError("offline")))
    import hyprial.daemon.impl.pac.views.context as context_module
    monkeypatch.setattr(context_module, "snapshot", lambda *args: pytest.fail("local fallback"))
    result = CliRunner().invoke(cli_app, ["workflow", "inspect", graph_id, "--node", "pm-helper", "--context", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "DAEMON_UNAVAILABLE"
    assert calls == [("workflow.node.context", {"runId": graph_id, "target": "pm-helper"})]
    assert store.path.is_file() and app._workflow_service is None


def test_formal_error_examples_match_real_socket(plan_socket, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hyprial.daemon.impl.pac.graphs import edits
    from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
    from hyprial.daemon.impl.request_catalog.registry import daemon_openapi
    from tests.daemon.workers.test_worker_relay import frame, request

    plan_socket.state_dir = tmp_path
    catalog = {entry["name"]: entry for entry in daemon_openapi()["x-hyprial-methods"]}
    cases = {case["name"]: case for case in catalog["workflow.node.context"]["x-hyprial-examples"]}
    for name in ("missing-target", "missing-store"):
        case = cases[name]
        assert request(plan_socket.socket_path, frame("workflow.node.context", case["params"]))["error"] == case["error"]
    store = PacGraphStore(default_database_path(tmp_path))
    try:
        monkeypatch.setattr(edits, "uuid4", lambda: SimpleNamespace(hex="11111111"))
        assert edits.create_graph(store, name="g", created_by="user:alice")["graphId"] == "g-11111111"
        case = cases["missing-node"]
        assert request(plan_socket.socket_path, frame("workflow.node.context", case["params"]))["error"] == case["error"]
        with sqlite3.connect(store.path) as db:
            db.execute("PRAGMA user_version=0")
        case = cases["resync"]
        assert request(plan_socket.socket_path, frame("workflow.node.context", case["params"]))["error"] == case["error"]
    finally:
        store.close()


def test_clock_guard_field_survives_real_registry_projection(plan_socket, tmp_path):
    from hyprial.daemon.impl.pac.graphs.edits import add_node, create_graph
    from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path

    plan_socket.state_dir = tmp_path
    store = PacGraphStore(default_database_path(tmp_path))
    try:
        graph_id = create_graph(store, name="clock", created_by="user:allen")["graphId"]
        version = add_node(store, tmp_path, graph_id=graph_id, node_id="task", owner="user:allen",
                           brief_ref="brief:task", expect_version=1)["version"]
        add_node(store, tmp_path, graph_id=graph_id, node_id="clock", owner="user:allen", brief_ref="brief:clock",
                 kind="clock", deadline_ms=1000, guarded_by_node_id="task", expect_version=version)
        result = daemon_request("workflow.node.context", {"runId": graph_id, "target": "clock"},
                                socket_path=plan_socket.socket_path, timeout=10.0)
        assert result["deadlineMs"] == 1000
        assert result["guardedByNodeId"] == "task"
    finally:
        store.close()
