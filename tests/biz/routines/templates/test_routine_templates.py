"""Built-in template CLI contracts; these commands do not require a daemon."""

import json

import pytest
import yaml
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.biz.impl.routine.templates import template_text


def test_templates_list_without_initialized_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "missing"))
    runner = CliRunner()
    result = runner.invoke(app, ["routine", "templates"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "agent-home-setup\nselfdrive\n"
    result = runner.invoke(app, ["routine", "templates", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "templates": ["agent-home-setup", "selfdrive"],
    }


def test_templates_show_packaged_yaml():
    result = CliRunner().invoke(app, ["routine", "templates", "show", "selfdrive"])
    assert result.exit_code == 0, result.output
    assert result.stdout == template_text("selfdrive")
    doc = yaml.safe_load(result.stdout)
    assert doc["source"] == {"kind": "pac-journal", "idle_threshold": "30m"}
    assert doc["policy"]["routes"] == [
        {"tag": "route:worker", "target_from": "tagarg"},
        {"tag": "route:self", "target": "self"},
        {"tag": "route:human", "escalate_to": "{{escalate_to}}"},
    ]
    assert doc["policy"]["default"] == "escalate"
    assert doc["limits"] == {
        "max_in_flight": 3,
        "circuit_breaker": {"window_runs": 5, "escalate_ratio": 1.0, "action": "pause+alarm"},
    }
    assert doc["on_task_timeout"] == {"action": "escalate", "escalate_to": "{{escalate_to}}"}
    assert "你被 routine 唤醒:{{reason}}" in doc["policy"]["task_template"]
    # U3: the packaged body must not teach the retired reply protocol -- the
    # dispatcher appends this task's own `flag set` command, and a template
    # that also said 「回复 DONE」 would contradict it inside one message.
    body = doc["policy"]["task_template"]
    assert "hyprial workflow complete" in body
    assert "DONE" not in body
    assert "BLOCKED" not in body
    assert "{{nonce}}" not in body
    assert "user:allen" not in result.stdout


def test_agent_home_setup_template_renders_for_a_borrowed_interactive_agent():
    from hyprial.biz.impl.routine.contracts.schema import load_routine_text
    from hyprial.biz.impl.routine.templates import render_template

    actor = "agent:owner:node:empty-agent"
    spec = load_routine_text(
        render_template(
            "agent-home-setup",
            owner=actor,
            escalate_to="user:owner",
            name="agent-home-setup-empty-agent",
        )
    )
    assert spec.mode == "scheduled"
    assert spec.interval_seconds == 900
    assert spec.role == "dispatch"
    assert spec.actor == actor
    assert spec.produces is None
    assert "agent home 还没有完成" in spec.task_template
    assert "hyprial routine add" in spec.task_template
    assert "hyprial routine set" in spec.task_template
    # Allen 2026-11-03: the first setup step binds a route to the owner of
    # every adapter pinned to this agent, so the agent can reach its owner.
    assert "hyprial adapter route candidates" in spec.task_template
    assert "hyprial adapter route add" in spec.task_template
    assert spec.task_template.index("hyprial adapter route add") < spec.task_template.index("hyprial routine add")


@pytest.mark.parametrize("name", ["unknown", "../selfdrive"])
def test_templates_show_unknown_rejected(name):
    result = CliRunner().invoke(app, ["routine", "templates", "show", name])
    assert result.exit_code == 1
    assert "unknown routine template" in result.output
    as_json = CliRunner().invoke(app, ["routine", "templates", "show", name, "--json"])
    assert as_json.exit_code == 1
    failure = json.loads(as_json.stdout)
    assert failure["ok"] is False and failure["code"] == "TEMPLATE_NOT_FOUND"
