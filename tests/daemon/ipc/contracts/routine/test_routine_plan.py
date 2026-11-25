"""CLI and daemon plan behavior across the real local IPC boundary."""

from __future__ import annotations

import json
import math
import socket
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from hyprial.biz import RoutineSchemaError, load_routine
from hyprial.kernel import RequestPortError, daemon_request, ipc_errors
from hyprial.shell.impl.cli.commands.common import support as cli_support
from hyprial.shell.impl.cli.commands.common.root import app as cli_app
from hyprial.shell.impl.daemon_wiring.routines import routine_text_loader
from tests.daemon.ipc.contracts.agent.test_relations_specs import app as app
from tests.integration.service_connect.test_service_connect_integration import _IdleRuntime
from tests.shell.cli.workflows.test_cli_routine import DOC


@pytest.fixture()
def plan_socket(app):
    app.socket_path = Path("/tmp") / f"plan-{uuid4().hex}.sock"
    app._runtime = _IdleRuntime()
    app._maintenance_generation = 1
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        yield app
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(2.0)
        app.socket_path.unlink(missing_ok=True)
        assert not dispatcher.is_alive()


def _request(app, text, label):
    return daemon_request("routine.plan", {"yaml": text, "label": label},
                          socket_path=app.socket_path, timeout=10.0)


def test_plan_real_socket_cli_matches_old_output(plan_socket, monkeypatch, tmp_path):
    file = tmp_path / "routine.yaml"
    file.write_text(DOC, encoding="utf-8")
    monkeypatch.setattr(cli_support, "_daemon_request",
        lambda method, params=None, **kwargs: daemon_request(method, params,
            socket_path=plan_socket.socket_path, timeout=10.0))
    result = CliRunner().invoke(cli_app, ["routine", "plan", str(file), "--json"])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)
    assert plan["ok"] is True
    assert plan["plan"]["name"] == "cli-routine"
    assert plan["plan"]["launch"] == load_routine(file).launch
    assert plan["plan"]["launch"] is not None
    assert plan["plan"]["source"] == {"kind": "taskwarrior", "filter": "+selfdrive -blocked"}
    text = CliRunner().invoke(cli_app, ["routine", "plan", str(file)])
    assert text.exit_code == 0, text.output
    assert text.stdout == (
        "routine: cli-routine — every 900s, source taskwarrior filter '+selfdrive -blocked'\n"
        "  route: tag 'route:self' -> self self\n"
        "  default: escalate; escalate_to: user:allen\n"
        "  limits: max_in_flight 3, circuit_breaker 5 runs @ ratio 1.0 -> pause+alarm\n"
    )


def test_bad_yaml_keeps_multiline_label_and_missing_file_is_local(plan_socket, monkeypatch, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 2\nname: [", encoding="utf-8")
    with pytest.raises(RoutineSchemaError) as old:
        load_routine(bad)
    with pytest.raises(RequestPortError) as remote:
        _request(plan_socket, bad.read_text(encoding="utf-8"), str(bad))
    assert remote.value.code == "ROUTINE_SCHEMA_ERROR"
    assert str(remote.value) == f"routine schema error: {old.value}"
    assert "\n" in str(remote.value)
    monkeypatch.setattr(cli_support, "_daemon_request", lambda *a, **k: pytest.fail("request before file read"))
    missing = tmp_path / "missing.yaml"
    result = CliRunner().invoke(cli_app, ["routine", "plan", str(missing), "--json"])
    assert result.exit_code == 1
    failure = json.loads(result.stdout)
    assert failure["code"] == "ROUTINE_SCHEMA_ERROR"
    assert failure["error"].startswith(f"routine schema error: cannot read {missing}:")


@pytest.mark.parametrize("value", [".nan", ".inf"])
def test_nonfinite_duration_preserves_existing_projection(plan_socket, tmp_path, value):
    file = tmp_path / "number.yaml"
    text = DOC.replace("15m", value)
    file.write_text(text, encoding="utf-8")
    old = load_routine(file).interval_seconds
    current = _request(plan_socket, text, str(file))["plan"]["intervalSeconds"]
    assert (math.isnan(old) and math.isnan(current)) or old == current


def test_loader_keeps_default_label_and_accepts_explicit_label():
    from hyprial.daemon import RoutineSchemaPortError

    with pytest.raises(RoutineSchemaPortError) as default:
        routine_text_loader("name: daily")
    with pytest.raises(RoutineSchemaPortError) as named:
        routine_text_loader("name: daily", label="sample.yaml")
    assert str(default.value).startswith("routine.version")
    assert str(named.value).startswith("sample.yaml.version")


def test_daemon_unavailable_is_reported_after_read(monkeypatch, tmp_path):
    file = tmp_path / "routine.yaml"
    file.write_text(DOC, encoding="utf-8")
    calls = []

    def unavailable(method, params=None, **kwargs):
        calls.append((method, params))
        raise ipc_errors.DaemonUnavailableError("offline")

    monkeypatch.setattr(cli_support, "_daemon_request", unavailable)
    result = CliRunner().invoke(cli_app, ["routine", "plan", str(file), "--json"])
    assert result.exit_code == 1
    assert calls == [("routine.plan", {"yaml": DOC, "label": str(file)})]
    assert json.loads(result.stdout)["code"] == ipc_errors.DAEMON_UNAVAILABLE


def test_oversize_request_never_reaches_loader(plan_socket):
    calls = []
    original = plan_socket._routine_text_loader

    def loader(text, *, label="routine"):
        calls.append(label)
        return original(text, label=label)

    plan_socket._routine_text_loader = loader
    try:
        with pytest.raises((RequestPortError, ipc_errors.DaemonDisconnectedError, OSError)):
            _request(plan_socket, "x" * (9 * 1024 * 1024), "oversize.yaml")
        assert calls == []
    finally:
        plan_socket._routine_text_loader = original


def test_oversize_ipc_response_is_rejected():
    path = Path("/tmp") / f"plan-response-{uuid4().hex}.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def serve():
        conn, _ = listener.accept()
        with conn:
            conn.recv(65536)
            try:
                conn.sendall(b'{"version":1,"id":"fixed","result":{"data":"' +
                             b"x" * (9 * 1024 * 1024) + b'"}}\n')
            except (BrokenPipeError, ConnectionResetError):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with pytest.raises(RequestPortError) as caught:
            daemon_request("routine.plan", {"yaml": "", "label": "x"},
                           socket_path=path, timeout=10.0, uuid4=lambda: "fixed")
        assert caught.value.code == "IPC_RESPONSE_TOO_LARGE"
    finally:
        listener.close()
        thread.join(2.0)
        path.unlink(missing_ok=True)
    assert not thread.is_alive()


@pytest.mark.parametrize("yaml_text,source,first_line", [
    ("pac-journal", {"kind": "pac-journal", "idleThresholdSeconds": 120.0},
     "routine: daily — every 60s, source pac-journal filter ''"),
    ("scheduled", {"kind": "scheduled"},
     "routine: daily — every 60s, source scheduled filter ''"),
])
def test_other_source_modes_preserve_json_and_text(plan_socket, monkeypatch, tmp_path, yaml_text, source, first_line):
    from hyprial.daemon.impl.request_catalog.routine.plan_examples import _JOURNAL, _SCHEDULED

    text = _JOURNAL if yaml_text == "pac-journal" else _SCHEDULED
    path = tmp_path / "routine.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(cli_support, "_daemon_request",
        lambda method, params=None, **kwargs: daemon_request(method, params,
            socket_path=plan_socket.socket_path, timeout=10.0))
    runner = CliRunner()
    result = runner.invoke(cli_app, ["routine", "plan", str(path), "--json"])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)["plan"]
    assert plan["source"] == source
    if yaml_text == "scheduled":
        assert (plan["actor"], plan["actorOwnership"], plan["limits"]["maxInFlight"]) == (
            "agent:owner:node:worker", "borrowed", 1
        )
    text_result = runner.invoke(cli_app, ["routine", "plan", str(path)])
    assert text_result.exit_code == 0, text_result.output
    assert text_result.stdout.splitlines()[0] == first_line


def test_diagnostic_label_is_not_opened_and_restore_gate_is_retained(plan_socket):
    absent_label = f"/nonexistent/plan-{uuid4().hex}.yaml"
    assert _request(plan_socket, DOC, absent_label)["plan"]["name"] == "cli-routine"
    with pytest.raises(RequestPortError) as invalid:
        _request(plan_socket, "name: daily", absent_label)
    assert str(invalid.value) == (
        f"routine schema error: {absent_label}.version must be 1 (stored source format) or 2"
    )
    original = plan_socket._routine_text_loader
    calls = []

    def tracked(text, *, label="routine"):
        calls.append(label)
        return original(text, label=label)

    plan_socket._routine_text_loader = tracked
    plan_socket._restore_done.clear()
    try:
        with pytest.raises(ipc_errors.DaemonRestoringError):
            daemon_request("routine.plan", {"yaml": DOC, "label": absent_label},
                           socket_path=plan_socket.socket_path, timeout=10.0, restore_wait=0)
        assert calls == []
    finally:
        plan_socket._restore_done.set()
        plan_socket._routine_text_loader = original


def test_published_success_examples_match_real_daemon(plan_socket):
    from hyprial.daemon.impl.request_catalog.routine.plan_examples import EXAMPLES
    from hyprial.kernel import example_to_json

    for case in EXAMPLES[0].cases:
        example = example_to_json(case)
        if example["kind"] != "success":
            continue
        params = example["params"]
        assert _request(plan_socket, params["yaml"], params["label"]) == example["result"]


def test_worker_relay_does_not_expose_other_routine_methods():
    from hyprial.kernel.impl.contracts.channels.worker_channel import (
        WorkerBinding, WorkerChannelError, bound_request,
    )

    binding = WorkerBinding("agent:owner:node:worker", "session-1", "epoch-1")
    with pytest.raises(WorkerChannelError) as caught:
        bound_request({"version": 1, "id": "one", "method": "routine.remove",
                       "params": {"name": "daily"}}, binding)
    assert caught.value.code == ipc_errors.METHOD_NOT_FOUND
    assert str(caught.value) == "Method unavailable on the worker channel"
