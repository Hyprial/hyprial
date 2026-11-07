from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path
import pytest
import hyprial.daemon.impl.harnesses.agent_sdk as agent_sdk_module
from hyprial.identity.impl.agents.home.environment import CompleteChildEnvironment
from hyprial.identity.impl.agents.runtime.context import resolve_agent_runtime_context
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.agent_sdk import AGENT_SDK_VERSION, ClaudeAgentSdkProcess, IsolatedAgentSdkClient, create_claude_sdk_client
from hyprial.daemon.impl.harnesses.claude.runtime import CLAUDE_CREDENTIAL_MISSING, ClaudeRuntimeError, prepare_claude_runtime_context, validate_claude_auth_environment
from hyprial.daemon.impl.harnesses.claude import ClaudeConnector
from hyprial.daemon.impl.harnesses.model_provider import ModelProviderError

from ..support import (
    FakeClient,
    _configured_claude_launch,
)
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


def test_real_sdk_client_is_dependency_isolated_from_daemon_mcp_v2() -> None:
    spec = HarnessLaunchSpec(
        harness="claude",
        name="claude-worker",
        headless=True,
        cwd="/work",
        args=(
            "--model",
            "opus",
            "--permission-mode=acceptEdits",
            "--session-id",
            "must-not-win",
        ),
    )

    client = create_claude_sdk_client(spec)

    assert isinstance(client, IsolatedAgentSdkClient)
    assert "--isolated" in client.command
    assert "--no-project" in client.command
    assert f"claude-agent-sdk=={AGENT_SDK_VERSION}" in client.command
    assert client.options == {
        "cwd": "/work",
        "model": "opus",
        "extraArgs": {"permission-mode": "acceptEdits"},
    }


def test_agent_config_loads_redirected_user_project_local_under_strict_mcp(
    tmp_path: Path,
) -> None:
    registry, context, channel, launch = _configured_claude_launch(tmp_path)
    try:
        client = IsolatedAgentSdkClient(
            HarnessLaunchSpec(
                harness="claude",
                name="claude-worker",
                headless=True,
                cwd=str(tmp_path / "work"),
            ),
            worker_command=(sys.executable, "-c", "pass"),
            worker_channel=channel,
            complete_launch=launch,
        )

        assert client.options["settingSources"] == ["user", "project", "local"]
        assert client.options["strictMcpConfig"] is True
        assert client.options["mcpServers"] == {
            "harness-bridge": channel.mcp_server
        }
        assert client._runtime_context is context
        assert client._env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        assert (context.roots.native_root / "settings.json").read_text() == (
            '{"model":"p23-unit"}\n'
        )
        projects = context.roots.native_root / "projects"
        assert projects.is_symlink()
        assert projects.readlink() == context.roots.session_root
    finally:
        registry.close()


def test_p2_sdk_claude_refuses_legacy_home_deepseek_auth(tmp_path: Path) -> None:
    registry, context, channel, launch = _configured_claude_launch(tmp_path)
    try:
        legacy_auth = context.roots.tool_home / ".pi" / "agent" / "auth.json"
        legacy_auth.parent.mkdir(parents=True)
        legacy_auth.write_text(
            json.dumps({"deepseek": {"type": "api_key", "key": "legacy-bait"}}),
            encoding="utf-8",
        )
        with pytest.raises(ModelProviderError, match="agent-owned DEEPSEEK_API_KEY"):
            IsolatedAgentSdkClient(
                HarnessLaunchSpec(
                    harness="claude",
                    name="claude-worker",
                    headless=True,
                    cwd=str(tmp_path / "work"),
                    model_provider="deepseek",
                    model="deepseek-reasoner",
                ),
                worker_command=(sys.executable, "-c", "pass"),
                worker_channel=channel,
                complete_launch=launch,
            )
    finally:
        registry.close()


def test_p2_connector_claude_refuses_legacy_home_deepseek_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, context, _channel, launch = _configured_claude_launch(tmp_path)
    try:
        legacy_auth = context.roots.tool_home / ".pi" / "agent" / "auth.json"
        legacy_auth.parent.mkdir(parents=True)
        legacy_auth.write_text(
            json.dumps({"deepseek": {"type": "api_key", "key": "legacy-bait"}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "hyprial.daemon.impl.harnesses.claude.PtyHarnessProcess.spawn",
            lambda *_args, **_kwargs: pytest.fail(
                "P2 auth rejection must happen before Claude spawn"
            ),
        )
        with pytest.raises(ModelProviderError, match="agent-owned DEEPSEEK_API_KEY"):
            ClaudeConnector().launch(
                HarnessLaunchSpec(
                    harness="claude",
                    name="claude-worker",
                    headless=True,
                    cwd=str(tmp_path / "work"),
                    model_provider="deepseek",
                    model="deepseek-reasoner",
                ),
                complete_launch=launch,
            )
    finally:
        registry.close()


def test_agent_config_refuses_partial_or_relative_native_root(tmp_path: Path) -> None:
    spec = HarnessLaunchSpec(
        harness="claude",
        name="claude-worker",
        headless=True,
        cwd=str(tmp_path / "work"),
    )
    registry, context, channel, launch = _configured_claude_launch(tmp_path)
    try:
        with pytest.raises(ValueError, match="present on both"):
            IsolatedAgentSdkClient(
                spec,
                worker_command=(sys.executable, "-c", "pass"),
                complete_launch=launch,
            )

        with pytest.raises(ValueError, match="present on both"):
            IsolatedAgentSdkClient(
                spec,
                worker_channel=channel,
                worker_command=(sys.executable, "-c", "pass"),
                complete_launch=dataclasses.replace(launch, runtime_context=None),
            )

        other_context = dataclasses.replace(context, tool_profile_id="other-profile")
        with pytest.raises(ValueError, match="contexts differ"):
            IsolatedAgentSdkClient(
                spec,
                worker_channel=channel,
                worker_command=(sys.executable, "-c", "pass"),
                complete_launch=dataclasses.replace(
                    launch, runtime_context=other_context
                ),
            )

        bad_environment = launch.environment.for_exec()
        bad_environment["CLAUDE_CONFIG_DIR"] = "relative"
        bad_launch = dataclasses.replace(
            launch,
            environment=CompleteChildEnvironment(
                tuple(sorted(bad_environment.items()))
            ),
        )
        with pytest.raises(ValueError, match="runtime context for CLAUDE_CONFIG_DIR"):
            IsolatedAgentSdkClient(
                spec,
                worker_channel=channel,
                worker_command=(sys.executable, "-c", "pass"),
                complete_launch=bad_launch,
            )

        wrong_actor = dataclasses.replace(context, actor="agent:other:node:worker")
        wrong_channel = dataclasses.replace(channel, runtime_context=wrong_actor)
        wrong_launch = dataclasses.replace(launch, runtime_context=wrong_actor)
        with pytest.raises(ValueError, match="actor differs"):
            IsolatedAgentSdkClient(
                spec,
                worker_channel=wrong_channel,
                worker_command=(sys.executable, "-c", "pass"),
                complete_launch=wrong_launch,
            )
    finally:
        registry.close()


def test_claude_native_view_is_idempotent_and_rejects_drift(tmp_path: Path) -> None:
    registry, context, _channel, _launch = _configured_claude_launch(tmp_path)
    try:
        prepare_claude_runtime_context(context)
        prepare_claude_runtime_context(context)
        settings = context.roots.native_root / "settings.json"
        settings.write_text('{"model":"drifted"}\n', encoding="utf-8")

        with pytest.raises(ClaudeRuntimeError, match="projection drifted"):
            prepare_claude_runtime_context(context)
    finally:
        registry.close()


def test_claude_native_view_switches_to_a_new_config_revision(tmp_path: Path) -> None:
    registry, first, _channel, _launch = _configured_claude_launch(tmp_path)
    try:
        prepare_claude_runtime_context(first)
        source = first.roots.config_source / "native" / "claude" / "settings.json"
        source.write_text('{"model":"p23-revision-two"}\n', encoding="utf-8")
        second = resolve_agent_runtime_context(
            registry=registry,
            agent_name="claude-worker",
            harness="claude",
            cwd=str(tmp_path / "work"),
            tool_profile=TEST_TOOL_PROFILE,
        )
        assert second is not None
        assert second.config_revision != first.config_revision

        prepare_claude_runtime_context(second)

        settings = second.roots.native_root / "settings.json"
        assert settings.is_symlink()
        assert settings.readlink() == second.roots.projection_root / "settings.json"
        assert settings.read_text() == '{"model":"p23-revision-two"}\n'
    finally:
        registry.close()


def test_claude_native_view_rejects_session_root_collision(tmp_path: Path) -> None:
    registry, context, _channel, _launch = _configured_claude_launch(tmp_path)
    try:
        (context.roots.native_root / "projects").mkdir()

        with pytest.raises(ClaudeRuntimeError, match="authorized session root"):
            prepare_claude_runtime_context(context)
    finally:
        registry.close()


def test_claude_runtime_rejects_file_and_explicit_auth_conflict(
    tmp_path: Path,
) -> None:
    registry, context, channel, launch = _configured_claude_launch(tmp_path)
    try:
        credentials = context.roots.native_root / ".credentials.json"
        credentials.write_text("P23_AUTH_SENTINEL_NOT_A_SECRET\n", encoding="utf-8")
        os.chmod(credentials, 0o600)
        environment = launch.environment.for_exec()
        environment["ANTHROPIC_API_KEY"] = "P23_API_SENTINEL_NOT_A_SECRET"
        conflicting = dataclasses.replace(
            launch,
            environment=CompleteChildEnvironment(tuple(sorted(environment.items()))),
        )

        with pytest.raises(ClaudeRuntimeError, match="may not coexist"):
            IsolatedAgentSdkClient(
                HarnessLaunchSpec(
                    harness="claude",
                    name="claude-worker",
                    headless=True,
                    cwd=str(tmp_path / "work"),
                ),
                worker_command=(sys.executable, "-c", "pass"),
                worker_channel=channel,
                complete_launch=conflicting,
            )
    finally:
        registry.close()


def test_p2_sdk_claude_accepts_shared_oauth_token_environment(
    tmp_path: Path,
) -> None:
    registry, _context, channel, launch = _configured_claude_launch(tmp_path)
    token = "fake-shared-claude-oauth-token"
    try:
        environment = launch.environment.for_exec()
        environment.pop("ANTHROPIC_API_KEY", None)
        environment["CLAUDE_CODE_OAUTH_TOKEN"] = token
        granted = dataclasses.replace(
            launch,
            environment=CompleteChildEnvironment(tuple(sorted(environment.items()))),
        )

        client = IsolatedAgentSdkClient(
            HarnessLaunchSpec(
                harness="claude",
                name="claude-worker",
                headless=True,
                cwd=str(tmp_path / "work"),
            ),
            worker_command=(sys.executable, "-c", "pass"),
            worker_channel=channel,
            complete_launch=granted,
        )

        assert client._env["CLAUDE_CODE_OAUTH_TOKEN"] == token
    finally:
        registry.close()


def test_claude_runtime_treats_shared_oauth_token_as_explicit_auth(
    tmp_path: Path,
) -> None:
    registry, context, channel, launch = _configured_claude_launch(tmp_path)
    token = "fake-shared-claude-oauth-token"
    try:
        credentials = context.roots.native_root / ".credentials.json"
        credentials.write_text("fake-native-credential\n", encoding="utf-8")
        os.chmod(credentials, 0o600)
        environment = launch.environment.for_exec()
        environment.pop("ANTHROPIC_API_KEY", None)
        environment["CLAUDE_CODE_OAUTH_TOKEN"] = token
        conflicting = dataclasses.replace(
            launch,
            environment=CompleteChildEnvironment(tuple(sorted(environment.items()))),
        )

        with pytest.raises(ClaudeRuntimeError, match="may not coexist") as error:
            IsolatedAgentSdkClient(
                HarnessLaunchSpec(
                    harness="claude",
                    name="claude-worker",
                    headless=True,
                    cwd=str(tmp_path / "work"),
                ),
                worker_command=(sys.executable, "-c", "pass"),
                worker_channel=channel,
                complete_launch=conflicting,
            )
        assert token not in str(error.value)
    finally:
        registry.close()


def test_claude_runtime_rejects_multiple_explicit_auth_methods(
    tmp_path: Path,
) -> None:
    registry, _context, channel, launch = _configured_claude_launch(tmp_path)
    token = "fake-shared-claude-oauth-token"
    api_key = "fake-anthropic-api-key"
    try:
        environment = launch.environment.for_exec()
        environment.update(
            {
                "ANTHROPIC_API_KEY": api_key,
                "CLAUDE_CODE_OAUTH_TOKEN": token,
            }
        )
        conflicting = dataclasses.replace(
            launch,
            environment=CompleteChildEnvironment(tuple(sorted(environment.items()))),
        )

        with pytest.raises(ClaudeRuntimeError, match="conflicting explicit") as error:
            IsolatedAgentSdkClient(
                HarnessLaunchSpec(
                    harness="claude",
                    name="claude-worker",
                    headless=True,
                    cwd=str(tmp_path / "work"),
                ),
                worker_command=(sys.executable, "-c", "pass"),
                worker_channel=channel,
                complete_launch=conflicting,
            )
        assert token not in str(error.value)
        assert api_key not in str(error.value)
    finally:
        registry.close()


@pytest.mark.parametrize("value", ["fake-dead-oauth-token", ""])
def test_claude_runtime_refuses_dead_oauth_name_with_replacement(
    tmp_path: Path,
    value: str,
) -> None:
    registry, _context, channel, launch = _configured_claude_launch(tmp_path)
    try:
        environment = launch.environment.for_exec()
        environment["ANTHROPIC_OAUTH_TOKEN"] = value
        invalid = dataclasses.replace(
            launch,
            environment=CompleteChildEnvironment(tuple(sorted(environment.items()))),
        )

        with pytest.raises(ClaudeRuntimeError, match="CLAUDE_CODE_OAUTH_TOKEN") as error:
            IsolatedAgentSdkClient(
                HarnessLaunchSpec(
                    harness="claude",
                    name="claude-worker",
                    headless=True,
                    cwd=str(tmp_path / "work"),
                ),
                worker_command=(sys.executable, "-c", "pass"),
                worker_channel=channel,
                complete_launch=invalid,
            )
        assert value not in str(error.value) if value else True
    finally:
        registry.close()


def test_claude_runtime_rejects_missing_p2_credential(tmp_path: Path) -> None:
    native_root = tmp_path / "native-claude"
    native_root.mkdir()

    with pytest.raises(ClaudeRuntimeError) as raised:
        validate_claude_auth_environment(native_root, {})

    assert raised.value.code == CLAUDE_CREDENTIAL_MISSING
    assert str(native_root / ".credentials.json") in str(raised.value)

    credentials = native_root / ".credentials.json"
    credentials.touch()
    os.chmod(credentials, 0o600)
    with pytest.raises(ClaudeRuntimeError) as empty:
        validate_claude_auth_environment(native_root, {})
    assert empty.value.code == CLAUDE_CREDENTIAL_MISSING


def test_claude_runtime_accepts_each_supported_credential_source(
    tmp_path: Path,
) -> None:
    native_root = tmp_path / "native-claude"
    native_root.mkdir()
    credentials = native_root / ".credentials.json"
    credentials.write_text("P23_FILE_SENTINEL_NOT_A_SECRET\n", encoding="utf-8")
    os.chmod(credentials, 0o600)

    validate_claude_auth_environment(native_root, {})

    credentials.unlink()
    validate_claude_auth_environment(
        native_root,
        {"ANTHROPIC_AUTH_TOKEN": "P23_ENV_SENTINEL_NOT_A_SECRET"},
    )


def test_claude_runtime_preserves_credential_shape_rejections(
    tmp_path: Path,
) -> None:
    native_root = tmp_path / "native-claude"
    native_root.mkdir()
    credentials = native_root / ".credentials.json"
    credentials.write_text("P23_FILE_SENTINEL_NOT_A_SECRET\n", encoding="utf-8")
    os.chmod(credentials, 0o644)

    with pytest.raises(ClaudeRuntimeError, match="mode must be 0600") as wrong_mode:
        validate_claude_auth_environment(native_root, {})
    assert wrong_mode.value.code is None

    credentials.unlink()
    target = tmp_path / "credential-target"
    target.write_text("P23_FILE_SENTINEL_NOT_A_SECRET\n", encoding="utf-8")
    credentials.symlink_to(target)
    with pytest.raises(ClaudeRuntimeError, match="must be a regular file") as symlink:
        validate_claude_auth_environment(native_root, {})
    assert symlink.value.code is None


def test_headless_claude_rejects_missing_credential_before_retry_loop(
    tmp_path: Path,
) -> None:
    registry, context, channel, launch = _configured_claude_launch(tmp_path)
    environment = launch.environment.for_exec()
    environment.pop("ANTHROPIC_API_KEY")
    secret = "P23_ERROR_MUST_NOT_PRINT_THIS_SECRET"
    environment["UNRELATED_SECRET"] = secret
    missing_launch = dataclasses.replace(
        launch,
        environment=CompleteChildEnvironment(tuple(sorted(environment.items()))),
    )
    client_attempts = 0

    def client_factory() -> FakeClient:
        nonlocal client_attempts
        client_attempts += 1
        return FakeClient([])

    process: ClaudeAgentSdkProcess | None = None
    try:
        with pytest.raises(ClaudeRuntimeError) as raised:
            process = ClaudeAgentSdkProcess(
                HarnessLaunchSpec(
                    harness="claude",
                    name="claude-worker",
                    headless=True,
                    cwd=str(tmp_path / "work"),
                ),
                client_factory=client_factory,
                worker_channel=channel,
                complete_launch=missing_launch,
            )
    finally:
        if process is not None:
            process.stop()
        registry.close()

    assert raised.value.code == CLAUDE_CREDENTIAL_MISSING
    assert raised.value.permanent_start_failure is True
    assert str(context.roots.native_root / ".credentials.json") in str(raised.value)
    assert secret not in str(raised.value)
    assert client_attempts == 0


@pytest.mark.parametrize("removed", ["projects", "settings.json"])
def test_authority_prepared_claude_view_is_verified_without_recreation(tmp_path, removed):
    registry, context, _channel, _launch = _configured_claude_launch(tmp_path)
    try:
        prepare_claude_runtime_context(context)
        prepared = dataclasses.replace(context, authority_prepared=True)
        prepare_claude_runtime_context(prepared)
        missing = context.roots.native_root / removed
        missing.unlink()
        with pytest.raises(ClaudeRuntimeError, match="authority-prepared"):
            prepare_claude_runtime_context(prepared)
        assert not missing.exists() and not missing.is_symlink()
    finally:
        registry.close()


@pytest.mark.anyio
async def test_stale_authority_context_fails_before_agent_sdk_native_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, context, channel, launch = _configured_claude_launch(tmp_path)
    spawned = 0

    async def forbidden_spawn(*_args, **_kwargs):
        nonlocal spawned
        spawned += 1
        raise AssertionError("native spawn must not run")

    class RejectedCustody:
        def __enter__(self):
            raise RuntimeError("AGENT_RUNTIME_CONTEXT_STALE")

        def __exit__(self, *_args):
            return False

    try:
        prepare_claude_runtime_context(context)
        prepared = dataclasses.replace(
            context, authority_prepared=True, launch_token="stale-token"
        )
        channel = dataclasses.replace(channel, runtime_context=prepared)
        launch = dataclasses.replace(launch, runtime_context=prepared)
        client = IsolatedAgentSdkClient(
            HarnessLaunchSpec(
                harness="claude", name="claude-worker", headless=True,
                cwd=str(tmp_path / "work"),
            ),
            worker_command=(sys.executable, "-c", "pass"),
            worker_channel=channel,
            complete_launch=launch,
            runtime_launch_custody=lambda _context: RejectedCustody(),
        )
        monkeypatch.setattr(
            agent_sdk_module.asyncio, "create_subprocess_exec", forbidden_spawn
        )
        with pytest.raises(RuntimeError, match="AGENT_RUNTIME_CONTEXT_STALE"):
            await client.__aenter__()
        assert spawned == 0
        assert prepared.roots.projection_root.exists()
    finally:
        registry.close()


@pytest.mark.anyio
async def test_current_authority_context_holds_custody_through_agent_sdk_spawn(
    tmp_path: Path,
) -> None:
    registry, context, channel, launch = _configured_claude_launch(tmp_path)
    entered = exited = 0

    class Custody:
        def __enter__(self):
            nonlocal entered
            entered += 1

        def __exit__(self, *_args):
            nonlocal exited
            exited += 1

    code = (
        "import json,time; "
        "print(json.dumps({'type':'ready'}), flush=True); time.sleep(0.2)"
    )
    try:
        prepare_claude_runtime_context(context)
        prepared = dataclasses.replace(
            context, authority_prepared=True, launch_token="current-token"
        )
        channel = dataclasses.replace(channel, runtime_context=prepared)
        launch = dataclasses.replace(launch, runtime_context=prepared)
        client = IsolatedAgentSdkClient(
            HarnessLaunchSpec(
                harness="claude", name="claude-worker", headless=True,
                cwd=str(tmp_path / "work"),
            ),
            worker_command=(sys.executable, "-c", code),
            worker_channel=channel,
            complete_launch=launch,
            runtime_launch_custody=lambda _context: Custody(),
        )
        await client.__aenter__()
        assert client.pid is not None
        assert (entered, exited) == (1, 1)
        await client.__aexit__(None, None, None)
    finally:
        registry.close()
