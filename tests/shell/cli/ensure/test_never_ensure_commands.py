"""Commands that observe or control the daemon never start it (Z-4).

Decision: daemon-first-z4-never-ensure-20261117.  Every lifecycle, upgrade,
profile, login and meta command opts out at its ``execute`` call; a business
command keeps the default.  The keyword is checked on a recording output
boundary that never runs the operation, and the observable result is checked
with a request port that is always unreachable.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support
from hyprial.shell.impl.cli.runtime import entry

from .conftest import unavailable

NEVER_ENSURE = [
    ["daemon", "status"],
    ["daemon", "start"],
    ["daemon", "stop"],
    ["daemon", "restart"],
    ["daemon", "service", "status"],
    ["daemon", "service", "install"],
    ["daemon", "service", "uninstall"],
    ["daemon", "upgrade", "status"],
    ["daemon", "upgrade", "apply"],
    ["profile", "list"],
    ["profile", "show"],
    ["profile", "use", "example"],
    ["profile", "create", "example", "--issuer", "https://issuer.example.test", "--client-id", "example"],
    ["login"],
    ["logout"],
    ["init"],
    ["version"],
    ["help"],
    ["socialware", "install"],
    ["adapter", "remove", "example"],
    ["agent", "tiers"],
    ["agent", "tiers", "probe"],
]


@pytest.fixture
def recorded_executes(monkeypatch):
    calls: list[dict] = []

    def record(operation, *, json_output, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(support, "_execute", record)
    return calls


@pytest.mark.parametrize("arguments", NEVER_ENSURE, ids=lambda value: " ".join(value))
def test_the_command_opts_out_of_the_implicit_start(recorded_executes, arguments):
    result = CliRunner().invoke(entry.app, [*arguments, "--json"])

    assert result.exit_code == 0, (result.output, result.exception)
    assert len(recorded_executes) == 1
    assert recorded_executes[0].get("ensure_daemon") is False


def test_a_business_command_keeps_the_default(recorded_executes):
    result = CliRunner().invoke(entry.app, ["agent", "list", "--json"])

    assert result.exit_code == 0, (result.output, result.exception)
    assert len(recorded_executes) == 1
    assert "ensure_daemon" not in recorded_executes[0]


def test_daemon_status_reports_absence_without_starting(ensure_env, monkeypatch):
    monkeypatch.delenv("HYPRIAL_DAEMON_AUTOSTART", raising=False)
    ensure_env.script = [unavailable()] * 4

    result = CliRunner().invoke(entry.app, ["daemon", "status", "--json"])

    assert result.exit_code == 0, (result.output, result.exception)
    payload = json.loads(result.stdout)
    assert payload["ok"] is True and payload["daemon"]["running"] is False
    assert ensure_env.launches == []


def test_a_business_command_with_the_opt_out_removed_reaches_the_launch_once(ensure_env, monkeypatch):
    monkeypatch.delenv("HYPRIAL_DAEMON_AUTOSTART", raising=False)
    ensure_env.script = [unavailable(), unavailable()]

    result = CliRunner().invoke(entry.app, ["agent", "list", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "DAEMON_UNAVAILABLE"
    assert len(ensure_env.launches) == 1


def test_tiers_probe_never_starts_the_daemon(ensure_env, monkeypatch):
    import hyprial.daemon as daemon

    monkeypatch.delenv("HYPRIAL_DAEMON_AUTOSTART", raising=False)
    monkeypatch.setattr(
        daemon, "probe_capabilities",
        lambda *args, **kwargs: {"ok": True, "probe": "user-runtime-capability", "capabilities": []},
    )
    ensure_env.script = [unavailable()]

    result = CliRunner().invoke(entry.app, ["agent", "tiers", "probe", "--json"])

    assert result.exit_code == 0, (result.output, result.exception)
    assert json.loads(result.stdout)["probe"] == "user-runtime-capability"
    assert ensure_env.launches == [] and ensure_env.requests == []
