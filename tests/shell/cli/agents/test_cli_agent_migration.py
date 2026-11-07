"""CLI preconditions for agent-home migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


_AGENT_URI = "agent:hyprial-unit-owner:test-node:fixture-agent"
_MIGRATION_METHODS = {
    "preflight": "agent.migrate.preflight",
    "execute": "agent.migrate.execute",
    "rollback": "agent.migrate.rollback",
}


def _arguments(operation: str, manifest: Path) -> list[str]:
    arguments = ["agent", "home", "migrate", operation, "fixture-agent"]
    if operation == "preflight":
        arguments.extend(["--manifest", str(manifest)])
    else:
        arguments.extend(["--migration-id", "a" * 32])
    if operation == "rollback":
        arguments.extend(
            [
                "--window-id",
                "rollback-window",
                "--responsible-owner",
                "operator@example.test",
                "--expires-at-ms",
                "2000000000000",
            ]
        )
    arguments.append("--json")
    return arguments


@pytest.mark.parametrize("operation", ["preflight", "execute", "rollback"])
def test_active_bound_routine_blocks_every_migration_operation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: str
) -> None:
    calls: list[str] = []

    def request(method: str, params: dict[str, object], **_kwargs: object) -> dict[str, object]:
        calls.append(method)
        if method == "routine.list":
            assert params["all"] is True
            return {
                "routines": [
                    {"name": "resident-squire", "owner": _AGENT_URI, "enabled": True}
                ]
            }
        raise AssertionError(f"migration request was not guarded: {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(_cli_root_app, _arguments(operation, manifest))

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout) == {
        "ok": False,
        "code": "ROUTINE_ACTIVE",
        "error": (
            "enabled routine(s) resident-squire are bound to agent 'fixture-agent'; "
            "pause them with `hyprial routine pause <name>` and resume after the migration"
        ),
    }
    assert calls == ["routine.list"]


@pytest.mark.parametrize(
    ("row", "operation"),
    [
        (
            {"name": "paused-agent", "owner": _AGENT_URI, "enabled": False},
            "preflight",
        ),
        (
            {
                "name": "other-agent",
                "owner": "agent:hyprial-unit-owner:test-node:other-agent",
                "enabled": True,
            },
            "execute",
        ),
    ],
)
def test_migration_request_is_sent_when_no_enabled_routine_binds_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    row: dict[str, object],
    operation: str,
) -> None:
    calls: list[str] = []

    def request(method: str, _params: dict[str, object], **_kwargs: object) -> dict[str, object]:
        calls.append(method)
        if method == "routine.list":
            return {"routines": [row]}
        return {"ok": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(_cli_root_app, _arguments(operation, manifest))

    assert result.exit_code == 0, result.output
    assert calls == ["routine.list", _MIGRATION_METHODS[operation]]


def test_config_rebind_cli_sends_daemon_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "staging"
    source.mkdir()
    calls: list[tuple[str, dict[str, object]]] = []

    def request(
        method: str, params: dict[str, object], **_kwargs: object
    ) -> dict[str, object]:
        calls.append((method, params))
        return {"ok": True, "state": "validated", "dryRun": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "config",
            "rebind",
            "fixture-agent",
            "--source",
            str(source),
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "agent.config.rebind",
            {"name": "fixture-agent", "source": str(source), "dryRun": True},
        )
    ]


def test_config_rebind_cli_refuses_relative_staging_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def request(
        method: str, _params: dict[str, object], **_kwargs: object
    ) -> dict[str, object]:
        calls.append(method)
        return {"ok": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "config",
            "rebind",
            "fixture-agent",
            "--source",
            "relative-staging",
            "--json",
        ],
    )

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == "AGENT_CONFIG_REBIND_INVALID_SOURCE"
    assert calls == []


def test_config_rebind_rollback_cli_sends_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def request(
        method: str, params: dict[str, object], **_kwargs: object
    ) -> dict[str, object]:
        calls.append((method, params))
        return {"ok": True, "state": "rolled-back"}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "config",
            "rebind",
            "fixture-agent",
            "--rollback",
            "receipt-1",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "agent.config.rebind",
            {"name": "fixture-agent", "rollbackReceiptId": "receipt-1"},
        )
    ]


def test_migration_guard_checks_coordinator_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def request(method: str, _params: dict[str, object], **_kwargs: object) -> dict[str, object]:
        calls.append(method)
        if method == "routine.list":
            return {
                "routines": [
                    {
                        "name": "coordinator-bound",
                        "owner": "user:operator",
                        "coordinator": _AGENT_URI,
                        "enabled": True,
                    }
                ]
            }
        raise AssertionError(f"migration request was not guarded: {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(_cli_root_app, _arguments("rollback", manifest))

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == "ROUTINE_ACTIVE"
    assert "coordinator-bound" in result.stdout
    assert calls == ["routine.list"]
