"""Official effort selection must reach both harnesses and survive restart."""

from dataclasses import replace
import asyncio
import json
import shutil

import pytest
from typer.testing import CliRunner

from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.model_provider import (
    claude_provider_environment,
    codex_provider_configuration,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
from hyprial.shell.impl.cli.runtime import entry as cli


@pytest.mark.parametrize("provider", [None, "openai", "deepseek", "hyprial"])
@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"])
@pytest.mark.parametrize("legacy", [False, True])
def test_codex_explicit_effort(provider, effort, legacy):
    spec = HarnessLaunchSpec(
        "codex", "worker", True,
        model_provider=provider,
        model="openai/gpt-test" if provider == "hyprial" else None,
        args=("-c", f'model_reasoning_effort="{effort}"'),
    )
    restored = HarnessLaunchSpec.from_json(spec.to_json(), "launch")
    args, _ = codex_provider_configuration(
        restored, {"DEEPSEEK_API_KEY": "fake", "HYPRIAL_API_KEY": "fake"},
        allow_legacy_home_fallback=legacy,
    )
    assert [a for a in args if a.startswith("model_reasoning_effort=")] == [
        f'model_reasoning_effort="{effort}"'
    ]


@pytest.mark.parametrize("provider", [None, "anthropic", "deepseek", "hyprial"])
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
@pytest.mark.parametrize("legacy", [False, True])
def test_claude_explicit_effort(provider, effort, legacy):
    spec = HarnessLaunchSpec(
        "claude", "worker", True,
        model_provider=provider,
        model="anthropic/claude-test" if provider == "hyprial" else None,
        args=("--effort", effort),
    )
    env = claude_provider_environment(
        spec, {"DEEPSEEK_API_KEY": "fake", "HYPRIAL_API_KEY": "fake"},
        allow_legacy_home_fallback=legacy,
    )
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == effort


@pytest.mark.parametrize("harness,effort,expected", [
    ("codex", "max", ["-c", 'model_reasoning_effort="max"']),
    ("claude", "low", ["--effort", "low"]),
])
def test_start_official_effort_is_durable(monkeypatch, tmp_path, harness, effort, expected):
    calls = []
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, params=None, **kw:
                        calls.append((method, params or {})) or {"ok": True})
    monkeypatch.setattr(shutil, "which", lambda name: f"/fake/{name}")
    result = CliRunner().invoke(cli.app, [
        "agent", "start", harness, "--name", "effort", "--cwd", str(tmp_path),
        "--headless", "--effort", effort, "--json",
    ])
    assert result.exit_code == 0, result.output
    assert calls[-1][1]["effort"] == effort
    assert calls[-1][1]["args"] == []
    assert json.loads(result.stdout)["ok"] is True


@pytest.mark.parametrize("harness,effort", [("codex", "bogus"), ("claude", "ultra"), ("pi", "high")])
def test_start_rejects_invalid_effort_before_daemon(monkeypatch, harness, effort):
    calls = []
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *a, **kw: calls.append(a))
    result = CliRunner().invoke(cli.app, ["agent", "start", harness, "--name", "effort", "--effort", effort, "--json"])
    assert result.exit_code == 1
    assert "effort" in result.output
    assert calls == []


def test_legacy_provider_defaults_unchanged():
    for provider in ("deepseek", "hyprial"):
        spec = HarnessLaunchSpec("codex", "worker", True, model_provider=provider,
                                 model="vendor/model" if provider == "hyprial" else None)
        env = {"DEEPSEEK_API_KEY": "fake", "HYPRIAL_API_KEY": "fake"}
        assert 'model_reasoning_effort="high"' in codex_provider_configuration(spec, env)[0]
        assert claude_provider_environment(replace(spec, harness="claude"), env)["CLAUDE_CODE_EFFORT_LEVEL"] == "max"
    assert codex_provider_configuration(HarnessLaunchSpec("codex", "w", True), {}) == ((), {})
    assert claude_provider_environment(HarnessLaunchSpec("claude", "w", True), {}) == {}


@pytest.mark.parametrize("args", [
    ("-c", 'model_reasoning_effort="max"'),
    ("--config", "model_reasoning_effort=max"),
    ("--config=model_reasoning_effort='max'",),
    ("-c=model_reasoning_effort=\"max\"",),
    ("-cmodel_reasoning_effort=max",),
])
def test_effort_config_reaches_thread_start_and_resume(tmp_path, args):
    from hyprial.daemon.impl.harnesses.codex import CodexAppServerClient

    spec = HarnessLaunchSpec("codex", "worker", True, cwd=str(tmp_path), args=args)
    client = CodexAppServerClient(spec, command=("/fake/codex",), env={})
    calls = []

    async def request(method, params, **kwargs):
        calls.append((method, params))
        return {"thread": {"id": "effort-thread"}}

    client.request = request
    asyncio.run(client._start_thread())
    assert asyncio.run(client._resume_thread())
    assert [method for method, _ in calls] == ["thread/start", "thread/resume"]
    for _, payload in calls:
        assert payload["config"]["model_reasoning_effort"] == "max"
    assert 'model_reasoning_effort="max"' in client.command


def test_thread_effort_merges_with_p2_tool_policy(tmp_path):
    from hyprial.daemon.impl.harnesses.codex.app_server import _thread_execution_params
    from hyprial.daemon.impl.harnesses.codex.native_env import _thread_config

    execution = _thread_execution_params(("-c", 'model_reasoning_effort="low"', "--sandbox", "workspace-write"))
    config = _thread_config(tmp_path, execution, None, managed_environment=True)
    assert config["model_reasoning_effort"] == "low"
    assert config["allow_login_shell"] is False
    assert config["shell_environment_policy"] == {"inherit": "all"}
    with pytest.raises(ValueError, match="not supported"):
        _thread_execution_params(("-c", "some_other_override=true"))


def test_p2_native_openai_effort_is_independent_of_provider(tmp_path):
    from tests.daemon.harnesses.codex.native_home.support import _native_responses
    from hyprial.daemon.impl.harnesses.codex.native_env import _validate_codex_native_load

    native, cwd, initialize, config, account, skills = _native_responses(tmp_path)
    config["layers"].insert(0, {
        "name": {"type": "sessionFlags"}, "version": "effort-regression",
        "config": {"model_reasoning_effort": "max"},
    })
    evidence = _validate_codex_native_load(
        initialize=initialize, config_read=config, account_read=account,
        skills_list=skills, native_root=native, cwd=cwd, model_provider="openai",
    )
    assert "sessionFlags" in evidence.layer_types


def test_claude_sdk_and_connector_receive_explicit_effort(tmp_path):
    from hyprial.daemon.impl.harnesses.agent_sdk import IsolatedAgentSdkClient
    from hyprial.daemon.impl.harnesses.claude import ClaudeConnector

    spec = HarnessLaunchSpec("claude", "worker", True, cwd=str(tmp_path),
                             model_provider="deepseek", args=("--effort", "low"))
    client = IsolatedAgentSdkClient(spec, env={"DEEPSEEK_API_KEY": "fake"},
                                    worker_command=("/fake/claude-worker",))
    assert client._env["CLAUDE_CODE_EFFORT_LEVEL"] == "low"
    assert client.options["extraArgs"]["effort"] == "low"
    assert "--effort" in ClaudeConnector().build_argv(spec)


@pytest.mark.parametrize("harness,tail", [
    ("codex", ["-c", 'model_reasoning_effort="high"']),
    ("claude", ["--effort=high"]),
])
def test_official_and_native_effort_conflict_is_refused(monkeypatch, harness, tail):
    calls = []
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *a, **kw: calls.append(a))
    result = CliRunner().invoke(cli.app, ["agent", "start", harness, "--name", "effort",
                                        "--effort", "low", "--json", "--", *tail])
    assert result.exit_code == 1
    assert "supplied both" in result.output
    assert calls == []


@pytest.mark.parametrize("harness,expected", [
    ("codex", ("-c", 'model_reasoning_effort="low"')),
    ("claude", ("--effort", "low")),
])
def test_official_effort_reaches_interactive_launcher(monkeypatch, tmp_path, harness, expected):
    captured = []
    monkeypatch.setattr(_cli_get_services(), f"_start_interactive_{harness}", lambda **kw:
                        captured.append(kw) or {"ok": True})
    result = CliRunner().invoke(cli.app, ["agent", "start", harness, "--name", "effort",
                                        "--cwd", str(tmp_path), "--effort", "low", "--json"])
    assert result.exit_code == 0, result.output
    assert captured[0]["runtime_args"] == expected


def test_interactive_resume_effort_reaches_server(tmp_path):
    from hyprial.daemon.impl.harnesses.codex.app_server import CodexInteractiveAppServer

    server = CodexInteractiveAppServer(tmp_path / "app.sock", cwd=tmp_path)
    calls = []

    def request(method, params):
        calls.append((method, params))
        return {"thread": {"id": "saved-thread", "cwd": str(tmp_path), "turns": []}}

    server.request = request
    result = server.resume_thread("saved-thread", runtime_args=("-c", 'model_reasoning_effort="max"'))
    assert result[0] == "saved-thread"
    assert calls[0][0] == "thread/resume"
    assert calls[0][1]["config"]["model_reasoning_effort"] == "max"


def test_launch_spec_roundtrip_preserves_optional_effort():
    spec = HarnessLaunchSpec("codex", "worker", True, effort="max")
    restored = HarnessLaunchSpec.from_json(spec.to_json(), "launch")
    assert restored.effort == "max"
    assert restored.args == ()
    assert HarnessLaunchSpec.from_json(restored.to_json(), "launch").to_json() == restored.to_json()


def test_encoded_effort_is_consumed_before_desired_state_replay():
    spec = HarnessLaunchSpec("codex", "worker", True, effort="max")
    consumed = HarnessLaunchSpec.from_json(
        {
            **spec.to_json(),
            "args": ["-c", 'model_reasoning_effort="max"'],
            "effort": None,
        },
        "launch",
    )
    assert consumed.effort is None
    assert consumed.args == ("-c", 'model_reasoning_effort="max"')
    # A restart that re-submits to_json must not see native args + effort.
    replay = HarnessLaunchSpec.from_json(consumed.to_json(), "launch")
    assert replay.effort is None
    assert replay.args == consumed.args


def test_lifecycle_projection_drops_effort_but_keeps_encoded_args():
    from hyprial.daemon.impl.composition.views import harness_launch_projection
    from hyprial.daemon.impl.harnesses.actor.contracts import _launch_spec

    spec = HarnessLaunchSpec(
        "codex", "worker", True,
        args=("-c", 'model_reasoning_effort="max"'),
        effort=None,
    )
    restored = _launch_spec(harness_launch_projection(spec))
    assert restored.effort is None
    assert restored.args == spec.args
    assert restored.to_json() == spec.to_json()


def test_consumed_effort_survives_desired_state_replay(tmp_path):
    from hyprial.daemon.impl.desired_state import DesiredStateStore

    spec = HarnessLaunchSpec(
        "codex", "worker", True,
        args=("-c", 'model_reasoning_effort="max"'),
        effort=None,
    )
    store = DesiredStateStore(tmp_path / "desired.json")
    store.upsert_harness(spec)
    restored = store.load().harnesses[0]
    assert restored.effort is None
    assert restored.args == spec.args
    replay = HarnessLaunchSpec.from_json(restored.to_json(), "launch")
    assert replay.effort is None
    assert replay.args == spec.args


def test_managed_state_roundtrip_preserves_effort(tmp_path):
    from hyprial.daemon.impl.desired_state import DesiredStateStore

    spec = HarnessLaunchSpec("codex", "worker", True, args=("-c", 'model_reasoning_effort="max"'))
    store = DesiredStateStore(tmp_path / "desired.json")
    store.upsert_harness(spec)
    restored = store.load().harnesses[0]
    assert restored.args == spec.args
