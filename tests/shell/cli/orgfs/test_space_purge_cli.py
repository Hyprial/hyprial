from __future__ import annotations

import json

from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common.root import app


def test_fs_purge_requires_yes_in_json_mode(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    calls = []
    monkeypatch.setattr(
        "hyprial.shell.impl.cli.commands.common.support._daemon_request",
        lambda method, params=None, **_kwargs: calls.append((method, params)),
    )

    result = CliRunner().invoke(
        app, ["org", "fs", "purge", "run", "space-1", "--json"]
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "ok": False,
        "code": "USER_ACTION_REQUIRED",
        "error": (
            "purging space 'space-1' permanently removes only this node's local "
            "replica; re-run with --yes to confirm"
        ),
    }
    assert calls == []


def test_fs_purge_interactive_decline_does_not_call_daemon(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    calls = []
    monkeypatch.setattr(
        "hyprial.shell.impl.cli.commands.common.support._daemon_request",
        lambda method, params=None, **_kwargs: calls.append((method, params)),
    )

    result = CliRunner().invoke(
        app, ["org", "fs", "purge", "run", "space-1"], input="n\n"
    )

    assert result.exit_code == 1
    assert "local replica" in result.output
    assert calls == []


def test_fs_purge_yes_uses_space_purge_ipc_and_reports_local_only(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    calls = []

    def request(method, params=None, **_kwargs):
        calls.append((method, params or {}))
        return {"spaceId": "space-1", "purged": True, "localOnly": True}

    monkeypatch.setattr("hyprial.shell.impl.cli.commands.common.support._daemon_request", request)

    result = CliRunner().invoke(
        app, ["org", "fs", "purge", "run", "space-1", "--yes", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "spaceId": "space-1",
        "purged": True,
        "localOnly": True,
    }
    assert calls == [("orgfs.space_purge", {"spaceId": "space-1"})]
