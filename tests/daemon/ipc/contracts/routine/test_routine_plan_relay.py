"""Routine plan through the scoped managed-worker relay."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from hyprial.kernel import bound_request, daemon_request, ipc_errors
from hyprial.shell.impl.cli.commands.common import support as cli_support
from hyprial.shell.impl.cli.commands.common.root import app as cli_app
from tests.daemon.workers.test_worker_relay import frame, isolated as isolated, request
from tests.shell.cli.workflows.test_cli_routine import DOC


@pytest.fixture()
def plan_file(isolated):
    root, _, _, _ = isolated
    path = root / "routine.yaml"
    path.write_text(DOC, encoding="utf-8")
    return path


def test_worker_relay_plans_through_daemon_and_cli(isolated, plan_file, monkeypatch):
    _, app, relay, binding = isolated
    payload = {"yaml": DOC, "label": str(plan_file)}
    inner = frame("routine.plan", payload)
    method, stamped = bound_request(inner, binding)
    assert method == "routine.plan"
    assert stamped == {**payload, "actor": binding.actor, "sessionRef": binding.session_ref}
    reply = request(relay.socket_path, inner)
    assert reply["result"]["plan"]["name"] == "cli-routine"
    assert reply["result"]["plan"]["source"] == {"kind": "taskwarrior", "filter": "+selfdrive -blocked"}

    monkeypatch.setattr(cli_support, "_daemon_request",
        lambda method, params=None, **kwargs: daemon_request(method, params,
            socket_path=relay.socket_path, timeout=10.0))
    result = CliRunner().invoke(cli_app, ["routine", "plan", str(plan_file), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"ok": True, **reply["result"]}
    text = CliRunner().invoke(cli_app, ["routine", "plan", str(plan_file)])
    assert text.exit_code == 0, text.output
    assert text.stdout.startswith("routine: cli-routine — every 900s, source taskwarrior")
    assert app._worker_session_ref(binding.actor) == binding.session_ref


@pytest.mark.parametrize("key,value,code", [
    ("resourcePaths", ["/host/secret"], "INVALID_ARGUMENT"),
    ("actor", "agent:test:isolated:bob", "CALLER_NOT_AUTHORIZED"),
    ("sessionRef", "forged-session", "CALLER_NOT_AUTHORIZED"),
])
def test_worker_relay_plan_rejects_extra_or_forged_claims(isolated, key, value, code):
    _, app, relay, binding = isolated
    inner = frame("routine.plan", {"yaml": DOC, "label": "routine.yaml", key: value})
    assert request(relay.socket_path, inner)["error"]["code"] == code
    direct = frame("worker.channel.request", binding.envelope(inner))
    assert request(app.socket_path, direct)["error"]["code"] == code


@pytest.mark.parametrize("change", ["epoch", "session"])
def test_worker_relay_plan_rejects_stale_binding(isolated, change):
    _, app, relay, _ = isolated
    if change == "epoch":
        app.epoch = "replacement-daemon"
    else:
        app._harnesses.refs[("codex", "alice")] = "new-session"
    response = request(relay.socket_path, frame("routine.plan", {"yaml": DOC, "label": "r.yaml"}))
    assert response["error"]["code"] == ipc_errors.STALE_SESSION


def test_worker_relay_schema_error_is_sanitized(isolated):
    _, app, relay, binding = isolated
    secret = '/host/private/secret-routine.yaml'
    inner = frame('routine.plan', {'yaml': 'name: missing-version', 'label': secret})
    host = request(app.socket_path, frame('worker.channel.request', binding.envelope(inner)))
    assert host['error']['code'] == 'ROUTINE_SCHEMA_ERROR'
    assert secret in host['error']['message']
    guest = request(relay.socket_path, inner)
    assert guest['error'] == {'code': 'ROUTINE_SCHEMA_ERROR', 'message': 'Worker channel request failed'}
    assert secret not in json.dumps(guest)
