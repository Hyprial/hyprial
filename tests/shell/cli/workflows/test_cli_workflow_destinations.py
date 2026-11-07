"""B1 workflow destinations retain the former PAC read/maintenance contracts."""

from __future__ import annotations

from typing import Any

from typer.testing import CliRunner
from typer.main import get_command

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_workflow_read_and_maintenance_destinations_forward_their_methods(
    monkeypatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append((method, params))
        return {"ok": True}

    runner = CliRunner()
    workflow_commands = get_command(_cli_root_app).commands["workflow"].commands
    assert {"events", "inspect", "reset", "worker", "notify", "migration"} <= set(workflow_commands)
    commands = (
        ["workflow", "events", "--help"],
        ["workflow", "inspect", "--help"],
        ["workflow", "reset", "--help"],
        ["workflow", "notify", "resend", "--help"],
        ["workflow", "migration", "status", "--help"],
    )
    for command in commands:
        result = runner.invoke(_cli_root_app, command)
        assert result.exit_code == 0, result.output
    assert calls == []


def test_workflow_destinations_keep_read_only_and_reason_arguments(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, params, **_kwargs: (calls.append((method, params)) or {"ok": True}),
    )
    runner = CliRunner()
    assert runner.invoke(_cli_root_app, ["workflow", "events", "--help"]).exit_code == 0
    assert runner.invoke(_cli_root_app, ["workflow", "reset", "--help"]).exit_code == 0
    assert "--after" in runner.invoke(_cli_root_app, ["workflow", "events", "--help"]).stdout
    assert "--reason-ref" in runner.invoke(_cli_root_app, ["workflow", "reset", "--help"]).stdout
