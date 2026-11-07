"""CLI contract for `hyprial routine` (self-drive, M-impl step 2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

DOC = """
version: 1
name: cli-routine
schedule: {interval: 15m}
source: {kind: taskwarrior, filter: "+selfdrive -blocked"}
policy:
  routes: [{tag: "route:self", target: "self"}]
  default: escalate
  task_template: "【{{nonce}}】{{task.description}}"
limits: {max_in_flight: 3, circuit_breaker: {window_runs: 5, escalate_ratio: 1.0, action: "pause+alarm"}}
on_task_timeout: {action: escalate, escalate_to: "user:allen"}
"""


@pytest.fixture()
def routine_file(tmp_path: Path) -> Path:
    path = tmp_path / "routine.yaml"
    path.write_text(DOC, encoding="utf-8")
    return path


def test_plan_validates_and_expands(routine_file: Path) -> None:
    result = CliRunner().invoke(_cli_root_app, ["routine", "plan", str(routine_file), "--json"])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)["plan"]
    assert plan["name"] == "cli-routine"
    assert plan["intervalSeconds"] == 900.0
    assert plan["limits"]["circuitBreaker"]["action"] == "pause+alarm"


def test_plan_schema_error_is_a_standard_failure(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nname: x\n", encoding="utf-8")
    result = CliRunner().invoke(_cli_root_app, ["routine", "plan", str(bad)])
    assert result.exit_code == 1
    assert "routine schema error" in result.output
    as_json = CliRunner().invoke(_cli_root_app, ["routine", "plan", str(bad), "--json"])
    assert as_json.exit_code == 1
    failure = json.loads(as_json.stdout)
    assert failure["ok"] is False and failure["code"] == "ROUTINE_SCHEMA_ERROR"


def test_lifecycle_commands_hit_daemon(monkeypatch: pytest.MonkeyPatch, routine_file: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def request(method: str, params: dict[str, object], **_kwargs: object) -> dict[str, object]:
        calls.append((method, params))
        if method == "routine.add":
            return {"name": "cli-routine", "enabled": True}
        if method == "routine.list":
            return {"routines": [{"name": "cli-routine"}]}
        if method == "routine.status":
            return {"name": "cli-routine", "enabled": True}
        if method == "routine.pause":
            return {"name": "cli-routine", "enabled": False}
        if method == "routine.resume":
            return {"name": "cli-routine", "enabled": True}
        if method == "routine.set":
            return {"name": "cli-routine", "enabled": True}
        return {"name": "cli-routine", "removed": True}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    runner = CliRunner()
    assert runner.invoke(
        _cli_root_app, ["routine", "add", str(routine_file), "--from", "user:hyprial-unit-owner", "--json"]
    ).exit_code == 0
    assert runner.invoke(_cli_root_app, ["routine", "list", "--json"]).exit_code == 0
    assert runner.invoke(_cli_root_app, ["routine", "status", "cli-routine", "--json"]).exit_code == 0
    assert runner.invoke(_cli_root_app, ["routine", "pause", "cli-routine", "--json"]).exit_code == 0
    assert runner.invoke(_cli_root_app, ["routine", "resume", "cli-routine", "--json"]).exit_code == 0
    assert runner.invoke(
        _cli_root_app,
        ["routine", "set", "cli-routine", str(routine_file), "--json"],
    ).exit_code == 0
    assert runner.invoke(_cli_root_app, ["routine", "remove", "cli-routine", "--json"]).exit_code == 0
    assert [m for m, _ in calls] == [
        "routine.add",
        "routine.list",
        "routine.status",
        "routine.pause",
        "routine.resume",
        "routine.set",
        "routine.remove",
    ]
    _, add_params = calls[0]
    assert add_params["actor"] == "user:hyprial-unit-owner"
    assert "cli-routine" in str(add_params["yaml"])


def test_list_default_and_all_scope_render_human_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    now_ms = 20_000_000
    monkeypatch.setattr(_cli_get_services().time, "time_ns", lambda: now_ms * 1_000_000)

    def request(method: str, params: dict[str, object], **_kwargs: object) -> dict[str, object]:
        calls.append((method, params))
        return {
            "routines": [
                {
                    "name": "nightly[red]",
                    "owner": "agent:test:node:coordinator",
                    "mode": "scheduled",
                    "enabled": True,
                    "lastDispatchAtMs": now_ms - 42_000,
                    "lastOutcome": "done",
                    "nextDueMs": now_ms + 37 * 60 * 1000,
                    "inFlight": [{"runId": "wf-one"}],
                }
            ]
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    runner = CliRunner()
    default = runner.invoke(_cli_root_app, ["routine", "list"])
    all_result = runner.invoke(_cli_root_app, ["routine", "list", "--all"])

    assert default.exit_code == 0, default.output
    assert "hyprial routine list   1 routines" in default.stdout
    assert "NAME" in default.stdout
    assert "LAST RUN" in default.stdout
    assert "nightly[red]" in default.stdout
    assert "42s" in default.stdout
    assert "done" in default.stdout
    assert "in 37m" in default.stdout
    assert default.stdout.splitlines()[-1].rstrip().endswith("1")
    assert all(len(line) <= 120 for line in default.stdout.splitlines())
    assert "all" not in calls[0][1]
    assert all_result.exit_code == 0, all_result.output
    assert "(all callers)" in all_result.stdout
    assert calls[1][1]["all"] is True
