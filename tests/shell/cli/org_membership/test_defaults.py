"""CLI sends optional org, never chooses a space from local membership rows."""

import json

from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support
from hyprial.shell.impl.cli.commands.common.root import app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401


def test_work_thin_default_and_explicit(monkeypatch, tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"version": 1, "owner": "alice"}))
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path))
    calls = []

    def request(method, params=None, **kwargs):
        calls.append((method, params))
        if method == "org.resolve":
            return {"spaceId": "space-b", "notices": ["Cleared unavailable current org b."]}
        assert method == "orgfs.ls"
        assert params == {"spaceId": "space-b", "path": "work"}
        return {"nodes": []}

    monkeypatch.setattr(support, "_daemon_request", request)
    for options in ([], ["--org", "a"]):
        result = CliRunner().invoke(app, ["org", "work", "ls", *options, "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["ok"]
        assert "Cleared" in result.stderr
        assert calls[-2] == ("org.resolve", {"org": "a"} if options else {})
        assert calls[-1] == ("orgfs.ls", {"spaceId": "space-b", "path": "work"})


def test_org_use_is_daemon_owned(monkeypatch, tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"version": 1, "owner": "alice"}))
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path))
    calls = []

    def request(method, params=None, **kwargs):
        calls.append((method, params))
        return {"ok": True, "current": params.get("org"), "notices": []}

    monkeypatch.setattr(support, "_daemon_request", request)
    for args, params in [(["b"], {"org": "b"}), (["--clear"], {"clear": True})]:
        result = CliRunner().invoke(app, ["org", "use", *args, "--json"])
        assert result.exit_code == 0, result.output
        assert calls[-1] == ("org.use", params)


def test_stale_current_notice_and_error_survive_lazy_org_resolution(monkeypatch, tmp_path):
    from hyprial.shell.impl.cli.commands.common.services import get_services

    (tmp_path / "settings.json").write_text(json.dumps({"version": 1, "owner": "alice"}))
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path))
    calls = []
    message = "Cleared unavailable current org group-stale."

    def request(method, params=None, **kwargs):
        calls.append((method, params))
        raise get_services().CliError("ORG_REQUIRED", "Choose an organization", {
            "candidates": [], "notices": [message],
        })

    monkeypatch.setattr(support, "_daemon_request", request)
    result = CliRunner().invoke(app, ["org", "work", "ls", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "ORG_REQUIRED"
    assert message in result.stderr
    assert calls == [("org.resolve", {})]
