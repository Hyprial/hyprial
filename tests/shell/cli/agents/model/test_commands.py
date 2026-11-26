"""CLI sends exact model choices and preserves uncertain operation errors."""

import json

from typer.testing import CliRunner

from hyprial.kernel import RequestPortError
from hyprial.shell.impl.cli.commands.common import support
from hyprial.shell.impl.cli.commands.common.root import app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401


def test_configuration_sends_exact_arguments_once(monkeypatch):
    calls = []

    def request(method, params=None, **kwargs):
        calls.append((method, params))
        return {"ok": True, "binding": {"bindingRevision": 2},
                "operation": {"operationId": "op-1", "phase": "finalized"}, "runtime": "not-verified"}

    monkeypatch.setattr(support, "_daemon_request", request)
    result = CliRunner().invoke(app, ["agent", "model", "configure", "worker",
        "--model", "hy4-flash", "--connection", "ref", "--config-revision", "3",
        "--binding-revision", "1", "--cell-revision", "4", "--operation-id", "op-1", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["operation"]["phase"] == "finalized"
    assert calls == [("agent.model.configure", {"name": "worker", "operationId": "op-1",
        "expectedBindingRevision": 1, "expectedCellRevision": 4, "configuration": {
            "authKind": "connection", "harness": "pi", "mode": "headless", "model": "hy4-flash",
            "connectionRef": "ref", "configRevision": 3}})]


def test_status_cancel_and_error_are_not_hidden_or_retried(monkeypatch):
    calls = []

    def request(method, params=None, **kwargs):
        calls.append((method, params))
        if method == "agent.model.cancel":
            raise RequestPortError("MODEL_CONFIGURATION_INCOMPLETE", "Unknown Cell result",
                                   {"phase": "bind", "reason": "cell-outcome-unknown"})
        return {"ok": True, "binding": None, "operation": None, "runtime": "not-verified"}

    monkeypatch.setattr(support, "_daemon_request", request)
    runner = CliRunner()
    result = runner.invoke(app, ["agent", "model", "status", "worker", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["runtime"] == "not-verified"
    result = runner.invoke(app, ["agent", "model", "cancel", "worker", "--operation-id", "op-1",
                                 "--binding-revision", "0", "--json"])
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["ok"] is False
    assert len(calls) == 2 and calls[1][0] == "agent.model.cancel"


def test_capacity_setting_is_offline_and_preserves_other_settings(tmp_path, monkeypatch):
    from hyprial.shell.impl.cli.commands.common.services import get_services

    monkeypatch.setattr(get_services(), "_hyprial_home", lambda: tmp_path)
    monkeypatch.setattr(support, "_daemon_request", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("offline setting must not contact the daemon")))
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"identity": {"owner": "fixture"}, "forwarding": {"mode": "off"}}))
    result = CliRunner().invoke(app, ["config", "set", "modelBinding.operationLimit", "20000", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["value"] == 20000
    assert json.loads(path.read_text()) == {"identity": {"owner": "fixture"},
        "forwarding": {"mode": "off"}, "modelBinding": {"operationLimit": 20000}}
    assert path.stat().st_mode & 0o777 == 0o600
