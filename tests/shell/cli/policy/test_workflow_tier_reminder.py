"""PAC tier reminders are policy data and do not change dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


REMINDER = "Choose the actor tier per node role: fast for execution; strong for planning and design."


@pytest.fixture
def workflow_file(tmp_path: Path) -> Path:
    path = tmp_path / "workflow.yaml"
    path.write_text(
        "version: 2\nname: cli-work\nnodes: [{id: work, task: Implement the task.}]\n",
        encoding="utf-8",
    )
    return path


def _policy(home: Path, reminder: str) -> None:
    (home / "dispatch-policy.json").write_text(
        json.dumps({"version": 1, "workflowReminder": reminder}) + "\n",
        encoding="utf-8",
    )


def test_plan_shows_configured_reminder(workflow_file, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _policy(home, REMINDER)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))

    result = CliRunner().invoke(_cli_root_app, ["workflow", "plan", str(workflow_file), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["reminders"] == [REMINDER]


def test_run_json_carries_configured_reminder(workflow_file, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _policy(home, REMINDER)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, params, **_kwargs: {"graphId": "wf-test", "runId": "wf-test"},
    )

    result = CliRunner().invoke(
        _cli_root_app, ["workflow", "run", str(workflow_file), "--yes", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["reminders"] == [REMINDER]
    assert result.stderr == ""


def test_human_run_prints_reminder_on_stderr_only(workflow_file, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _policy(home, REMINDER)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, params, **_kwargs: {"graphId": "wf-test", "runId": "wf-test"},
    )

    result = CliRunner().invoke(_cli_root_app, ["workflow", "run", str(workflow_file), "--yes"])

    assert result.exit_code == 0, result.output
    assert REMINDER not in result.stdout
    assert REMINDER in result.stderr


def test_empty_configured_reminder_has_empty_reminders(workflow_file, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _policy(home, "")
    monkeypatch.setenv("HYPRIAL_HOME", str(home))

    result = CliRunner().invoke(_cli_root_app, ["workflow", "plan", str(workflow_file), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["reminders"] == []


def test_changing_policy_changes_reminder_without_code_change(workflow_file, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    _policy(home, "first")
    first = CliRunner().invoke(_cli_root_app, ["workflow", "plan", str(workflow_file), "--json"])
    _policy(home, "second")
    second = CliRunner().invoke(_cli_root_app, ["workflow", "plan", str(workflow_file), "--json"])

    assert json.loads(first.stdout)["reminders"] == ["first"]
    assert json.loads(second.stdout)["reminders"] == ["second"]


def test_agent_tiers_exposes_reminder(workflow_file, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _policy(home, REMINDER)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))

    result = CliRunner().invoke(_cli_root_app, ["agent", "tiers", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["reminders"] == [REMINDER]


def test_reminder_does_not_change_dispatch_request(workflow_file, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    calls = []
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, params, **_kwargs: (
            calls.append((method, params.copy()))
            or {"graphId": "wf-test", "runId": "wf-test"}
        ),
    )

    without = CliRunner().invoke(
        _cli_root_app, ["workflow", "run", str(workflow_file), "--yes", "--json"]
    )
    _policy(home, REMINDER)
    with_reminder = CliRunner().invoke(
        _cli_root_app, ["workflow", "run", str(workflow_file), "--yes", "--json"]
    )

    assert without.exit_code == with_reminder.exit_code == 0
    assert calls[0] == calls[1]
    assert json.loads(without.stdout)["graphId"] == json.loads(with_reminder.stdout)["graphId"]


def test_home_without_policy_file_gets_the_shipped_default(workflow_file, tmp_path, monkeypatch):
    # Every live install was initialized before this policy existed; those
    # homes must show the shipped reminder, not silently drop it.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    assert not (home / "dispatch-policy.json").exists()

    result = CliRunner().invoke(_cli_root_app, ["workflow", "plan", str(workflow_file), "--json"])

    assert result.exit_code == 0, result.output
    [reminder] = json.loads(result.stdout)["reminders"]
    assert "fast tier (kimi / glm / deepseek)" in reminder
    assert "strong tier (gpt / claude)" in reminder


@pytest.mark.parametrize(
    "corrupt",
    [
        "{not json",
        json.dumps({"version": 99, "workflowReminder": "x"}),
        json.dumps({"version": 1, "workflowReminder": 42}),
    ],
)
def test_corrupt_policy_never_stops_a_dispatch(workflow_file, tmp_path, monkeypatch, corrupt):
    # The reminder is advice: a broken operator policy yields no reminder
    # plus the reason, and the graph is still dispatched unchanged.
    home = tmp_path / "home"
    home.mkdir()
    (home / "dispatch-policy.json").write_text(corrupt, encoding="utf-8")
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    calls = []
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, params, **_kwargs: (
            calls.append(method) or {"graphId": "wf-test", "runId": "wf-test"}
        ),
    )

    run = CliRunner().invoke(_cli_root_app, ["workflow", "run", str(workflow_file), "--yes", "--json"])
    assert run.exit_code == 0, run.output
    body = json.loads(run.stdout)
    assert calls == ["workflow.start"]
    assert body["graphId"] == "wf-test"
    assert body["reminders"] == []
    assert body["reminderError"]

    human = CliRunner().invoke(_cli_root_app, ["workflow", "run", str(workflow_file), "--yes"])
    assert human.exit_code == 0, human.output
    assert "reminder unavailable:" in human.stderr

    plan = CliRunner().invoke(_cli_root_app, ["workflow", "plan", str(workflow_file), "--json"])
    assert plan.exit_code == 0, plan.output
    assert json.loads(plan.stdout)["reminderError"]

    # Where policy is managed, the error stays loud.
    matrix = CliRunner().invoke(_cli_root_app, ["agent", "tiers", "--json"])
    assert matrix.exit_code != 0


@pytest.mark.parametrize(
    "corrupt",
    [
        "{not json",
        json.dumps({"version": 99, "workflowReminder": "x"}),
        json.dumps({"version": 1, "workflowReminder": 42}),
    ],
)
def test_config_set_repairs_a_broken_policy_file(tmp_path, monkeypatch, corrupt):
    # `config set` is the repair path; it must overwrite a file it cannot read.
    home = tmp_path / "home"
    home.mkdir()
    (home / "dispatch-policy.json").write_text(corrupt, encoding="utf-8")
    monkeypatch.setenv("HYPRIAL_HOME", str(home))

    result = CliRunner().invoke(
        _cli_root_app, ["config", "set", "dispatch.reminder", "repaired", "--json"]
    )

    assert result.exit_code == 0, result.output
    matrix = CliRunner().invoke(_cli_root_app, ["agent", "tiers", "--json"])
    assert matrix.exit_code == 0, matrix.output
    assert json.loads(matrix.stdout)["reminders"] == ["repaired"]


def test_misspelt_policy_key_is_named_not_ignored(workflow_file, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "dispatch-policy.json").write_text(
        json.dumps({"version": 1, "workflowReminder": "ok", "workflowReminderr": "typo"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HYPRIAL_HOME", str(home))

    plan = CliRunner().invoke(_cli_root_app, ["workflow", "plan", str(workflow_file), "--json"])

    assert plan.exit_code == 0, plan.output
    assert "workflowReminderr" in json.loads(plan.stdout)["reminderError"]
