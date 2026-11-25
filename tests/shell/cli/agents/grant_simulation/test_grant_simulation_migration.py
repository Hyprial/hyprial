"""CLI compatibility and daemon delegation for grant simulations."""

import json

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support
from hyprial.shell.impl.cli.runtime import entry as cli
from tests.daemon.ipc.contracts.agent.grant_simulation.test_grant_simulation import online as online, server as server, app as app


@pytest.mark.parametrize(("verb", "extra", "message"), [
    ("check", ["--capability", "tool-surface", "--resource", "shell"],
     "principal uri must be a non-empty, unpadded string"),
    ("visibility", ["--target", "user:someone"],
     "caller uri must be a non-empty, unpadded string"),
])
def test_invalid_simulated_caller_retains_uncoded_failure(online, verb, extra, message):
    result = CliRunner().invoke(cli.app, ["agent", "grant", "ignored", verb, *extra,
                                         "--caller", " ", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"ok": False, "error": message}


def test_operator_simulation_ignores_unused_scope_and_revision(online):
    result = CliRunner().invoke(cli.app, ["agent", "grant", "ignored", "check",
        "--capability", "tool-surface", "--resource", "shell", "--scope", "not-json",
        "--revision", "0", "--hosted", "--unverified", "--json"])
    assert result.exit_code == 0, result.output
    result_json = json.loads(result.stdout)
    assert result_json["allowed"] is True
    assert result_json["caller"] is None


@pytest.mark.parametrize(("verb", "extra", "method"), [
    ("check", ["--capability", "tool-surface", "--resource", "shell"], "agent.grant.check"),
    ("visibility", ["--target", "user:someone"], "agent.grant.visibility"),
])
def test_simulation_dispatches_to_daemon(monkeypatch, verb, extra, method):
    calls = []

    def request(name, params=None, **kwargs):
        calls.append((name, params))
        return {"sentinel": "daemon-result"}

    monkeypatch.setattr(support, "_daemon_request", request)
    result = CliRunner().invoke(cli.app, ["agent", "grant", "ignored", verb, *extra, "--json"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1 and calls[0][0] == method
    assert json.loads(result.stdout) == {"ok": True, "sentinel": "daemon-result"}


def test_missing_capability_is_reported_before_request(monkeypatch):
    monkeypatch.setattr(support, '_daemon_request', lambda *a, **k: pytest.fail('request before required argument'))
    result = CliRunner().invoke(cli.app, ['agent', 'grant', 'ignored', 'check', '--resource', 'shell', '--json'])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {'ok': False, 'code': 'INVALID_ARGUMENT',
                                         'error': '--capability is required for this verb'}
