from __future__ import annotations

import copy
import tomllib
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.codex.launch import build_codex_launch_inputs
from hyprial.daemon.impl.harnesses.codex.launch import inputs as launch_inputs_module
from hyprial.daemon.impl.harnesses.codex.native_env import (
    CodexAgentHomeError,
    _validate_codex_native_load,
)
from hyprial.kernel import HarnessLaunchSpec

from ..native_home.support import _native_responses


ACTOR = "agent:owner:node:codex-tui"


def _session_flags(argv: tuple[str, ...]) -> dict[str, object]:
    assert len(argv) % 2 == 0
    assert argv[::2] == ("-c",) * (len(argv) // 2)
    return tomllib.loads("\n".join(argv[1::2]))


def _inputs(tmp_path: Path, provider: str):
    model = "deepseek-flash" if provider == "deepseek" else None
    return build_codex_launch_inputs(
        HarnessLaunchSpec(
            "codex",
            "codex-tui",
            False,
            model_provider=provider,
            model=model,
        ),
        {"DEEPSEEK_API_KEY": "not-a-real-provider-key"},
        tool_profile=True,
        interactive_channel={
            "actor": ACTOR,
            "session_ref_file": tmp_path / "session-ref",
            "hyprial_home": tmp_path / "hyprial",
            "state_dir": tmp_path / "state",
        },
        allow_legacy_home_fallback=False,
    )


def _responses_for(
    tmp_path: Path,
    *,
    provider: str,
    keep_auth: bool = False,
) -> tuple[Path, Path, object, dict[str, object], object, object, object]:
    native, cwd, initialize, config_read, account_read, skills = _native_responses(
        tmp_path
    )
    inputs = _inputs(tmp_path, provider)
    observed = _session_flags(inputs.argv)
    assert observed == inputs.expected_session_flags
    provider_config = _session_flags(inputs.provider_argv)
    provider_keys = {
        "model",
        "model_provider",
        "model_providers",
        "model_reasoning_effort",
    }
    expected_provider_keys = provider_keys if provider == "deepseek" else set()
    assert set(observed).intersection(provider_keys) == expected_provider_keys
    config_read["layers"].insert(
        0,
        {"name": {"type": "sessionFlags"}, "config": copy.deepcopy(observed)},
    )
    if provider == "deepseek":
        config_read["config"].update(copy.deepcopy(provider_config))
        config_read["config"]["cli_auth_credentials_store"] = "ephemeral"
        account_read = {"account": None, "requiresOpenaiAuth": False}
        if not keep_auth:
            (native / "auth.json").unlink()
    else:
        config_read["config"]["cli_auth_credentials_store"] = "file"
    return native, cwd, initialize, config_read, account_read, skills, inputs


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_interactive_launch_argv_is_the_exact_validated_layer(
    tmp_path: Path, provider: str
) -> None:
    native, cwd, initialize, config_read, account_read, skills, inputs = _responses_for(
        tmp_path, provider=provider
    )

    evidence = _validate_codex_native_load(
        initialize=initialize,
        config_read=config_read,
        account_read=account_read,
        skills_list=skills,
        native_root=native,
        cwd=cwd,
        model_provider=provider,
        expected_session_flags=inputs.expected_session_flags,
    )

    assert evidence.auth_store == ("ephemeral" if provider == "deepseek" else "file")
    servers = inputs.expected_session_flags["mcp_servers"]
    assert isinstance(servers, dict) and set(servers) == {"harness-bridge"}
    bridge = servers["harness-bridge"]
    assert isinstance(bridge, dict) and set(bridge) == {"command", "args", "env"}
    command = inputs.app_server_command(("codex",), "--listen", "test")
    app_server = command.index("app-server")
    assert command[1:app_server] == ()
    assert command[app_server + 1 :] == (*inputs.argv, "--listen", "test")


def test_interactive_and_headless_pin_provider_session_contracts(
    tmp_path: Path,
) -> None:
    interactive = _inputs(tmp_path, "deepseek")
    headless = build_codex_launch_inputs(
        HarnessLaunchSpec(
            "codex",
            "codex-headless",
            True,
            model_provider="deepseek",
            model="deepseek-flash",
        ),
        {"DEEPSEEK_API_KEY": "not-a-real-provider-key"},
        provider_session_flags=True,
        allow_legacy_home_fallback=False,
    )
    # R&D ruling: loopback proved custom Codex starts require an ephemeral store.
    expected_provider_argv = (
        "-c",
        'cli_auth_credentials_store="ephemeral"',
        "-c",
        'model_provider="deepseek"',
        "-c",
        'model="deepseek-flash"',
        "-c",
        'model_reasoning_effort="high"',
        "-c",
        'model_providers.deepseek.name="deepseek"',
        "-c",
        'model_providers.deepseek.base_url="https://api.deepseek.com/"',
        "-c",
        'model_providers.deepseek.wire_api="responses"',
        "-c",
        'model_providers.deepseek.env_key="DEEPSEEK_API_KEY"',
        "-c",
        "model_providers.deepseek.requires_openai_auth=false",
    )
    expected_provider_flags = {
        "cli_auth_credentials_store",
        "model",
        "model_provider",
        "model_providers",
        "model_reasoning_effort",
    }

    assert interactive.provider_argv == expected_provider_argv
    assert headless.provider_argv == expected_provider_argv
    assert (
        expected_provider_flags.intersection(interactive.expected_session_flags)
        == expected_provider_flags
    )
    assert (
        expected_provider_flags.intersection(headless.expected_session_flags)
        == expected_provider_flags
    )
    assert _session_flags(interactive.provider_argv) == _session_flags(
        headless.provider_argv
    )
    assert {
        key: interactive.expected_session_flags[key]
        for key in expected_provider_flags
    } == _session_flags(interactive.provider_argv)

    openai = _inputs(tmp_path, "openai")
    assert openai.provider_argv == ()
    assert "cli_auth_credentials_store" not in openai.expected_session_flags


def test_injection_builder_rejects_incompatible_launch_modes(tmp_path: Path) -> None:
    headless = HarnessLaunchSpec("codex", "codex-headless", True)
    attended = HarnessLaunchSpec("codex", "codex-tui", False)
    channel = {
        "actor": ACTOR,
        "session_ref_file": tmp_path / "session-ref",
        "hyprial_home": tmp_path / "hyprial",
        "state_dir": tmp_path / "state",
    }

    with pytest.raises(ValueError, match="attended launch"):
        build_codex_launch_inputs(headless, {}, interactive_channel=channel)
    with pytest.raises(ValueError, match="selected automatically"):
        build_codex_launch_inputs(
            attended,
            {},
            provider_session_flags=True,
            interactive_channel=channel,
        )
    with pytest.raises(ValueError, match="headless launch"):
        build_codex_launch_inputs(attended, {}, provider_session_flags=True)


def test_extra_mcp_server_is_refused(tmp_path: Path) -> None:
    native, cwd, initialize, config_read, account_read, skills, inputs = _responses_for(
        tmp_path, provider="openai"
    )
    config_read["layers"][0]["config"]["mcp_servers"]["other"] = {
        "command": "unapproved"
    }

    with pytest.raises(CodexAgentHomeError, match="not an approved launch input"):
        _validate_codex_native_load(
            initialize=initialize,
            config_read=config_read,
            account_read=account_read,
            skills_list=skills,
            native_root=native,
            cwd=cwd,
            model_provider="openai",
            expected_session_flags=inputs.expected_session_flags,
        )


def test_harness_bridge_extra_key_is_refused(tmp_path: Path) -> None:
    native, cwd, initialize, config_read, account_read, skills, inputs = _responses_for(
        tmp_path, provider="openai"
    )
    config_read["layers"][0]["config"]["mcp_servers"]["harness-bridge"][
        "unapproved"
    ] = True

    with pytest.raises(
        CodexAgentHomeError,
        match=r"not an approved launch input: mcp_servers\.harness-bridge\.unapproved",
    ):
        _validate_codex_native_load(
            initialize=initialize,
            config_read=config_read,
            account_read=account_read,
            skills_list=skills,
            native_root=native,
            cwd=cwd,
            model_provider="openai",
            expected_session_flags=inputs.expected_session_flags,
        )


def test_harness_bridge_value_mismatch_is_refused(tmp_path: Path) -> None:
    native, cwd, initialize, config_read, account_read, skills, inputs = _responses_for(
        tmp_path, provider="openai"
    )
    config_read["layers"][0]["config"]["mcp_servers"]["harness-bridge"]["command"] = (
        "unapproved"
    )

    with pytest.raises(CodexAgentHomeError, match="does not match the approved"):
        _validate_codex_native_load(
            initialize=initialize,
            config_read=config_read,
            account_read=account_read,
            skills_list=skills,
            native_root=native,
            cwd=cwd,
            model_provider="openai",
            expected_session_flags=inputs.expected_session_flags,
        )


@pytest.mark.parametrize("layer_type", ["project", "user"])
def test_config_layer_harness_bridge_is_refused(
    tmp_path: Path, layer_type: str
) -> None:
    native, cwd, initialize, config_read, account_read, skills, inputs = _responses_for(
        tmp_path, provider="openai"
    )
    config_layer = next(
        layer
        for layer in config_read["layers"]
        if layer["name"]["type"] == layer_type
    )
    config_layer["config"]["mcp_servers"] = {
        "harness-bridge": copy.deepcopy(
            inputs.expected_session_flags["mcp_servers"]["harness-bridge"]
        )
    }

    with pytest.raises(CodexAgentHomeError, match="reserved MCP server"):
        _validate_codex_native_load(
            initialize=initialize,
            config_read=config_read,
            account_read=account_read,
            skills_list=skills,
            native_root=native,
            cwd=cwd,
            model_provider="openai",
            expected_session_flags=inputs.expected_session_flags,
        )


def test_mutated_injection_with_extra_key_is_still_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Validation stays independent when the injection builder is mutated."""

    original_config_argv = launch_inputs_module._config_argv

    def mutated_config_argv(flags: dict[str, object]) -> tuple[str, ...]:
        flags["unapproved"] = True
        return original_config_argv(flags)

    monkeypatch.setattr(launch_inputs_module, "_config_argv", mutated_config_argv)
    inputs = _inputs(tmp_path, "openai")
    observed = _session_flags(inputs.argv)
    assert observed["unapproved"] is True
    assert inputs.expected_session_flags["unapproved"] is True

    native, cwd, initialize, config_read, account_read, skills = _native_responses(
        tmp_path
    )
    config_read["layers"].insert(
        0,
        {"name": {"type": "sessionFlags"}, "config": observed},
    )

    with pytest.raises(
        CodexAgentHomeError, match="not an approved launch input: unapproved"
    ):
        _validate_codex_native_load(
            initialize=initialize,
            config_read=config_read,
            account_read=account_read,
            skills_list=skills,
            native_root=native,
            cwd=cwd,
            model_provider="openai",
            expected_session_flags=inputs.expected_session_flags,
        )


def test_unapproved_session_key_is_refused(tmp_path: Path) -> None:
    native, cwd, initialize, config_read, account_read, skills, inputs = _responses_for(
        tmp_path, provider="openai"
    )
    config_read["layers"][0]["config"]["unapproved"] = True

    with pytest.raises(
        CodexAgentHomeError, match="not an approved launch input: unapproved"
    ):
        _validate_codex_native_load(
            initialize=initialize,
            config_read=config_read,
            account_read=account_read,
            skills_list=skills,
            native_root=native,
            cwd=cwd,
            model_provider="openai",
            expected_session_flags=inputs.expected_session_flags,
        )


def test_provider_mismatch_is_refused(tmp_path: Path) -> None:
    native, cwd, initialize, config_read, account_read, skills, inputs = _responses_for(
        tmp_path, provider="deepseek"
    )
    config_read["config"]["model_provider"] = "other"

    with pytest.raises(CodexAgentHomeError, match="effective model provider"):
        _validate_codex_native_load(
            initialize=initialize,
            config_read=config_read,
            account_read=account_read,
            skills_list=skills,
            native_root=native,
            cwd=cwd,
            model_provider="deepseek",
            expected_session_flags=inputs.expected_session_flags,
        )


def test_custom_provider_with_auth_json_is_refused(tmp_path: Path) -> None:
    native, cwd, initialize, config_read, account_read, skills, inputs = _responses_for(
        tmp_path, provider="deepseek", keep_auth=True
    )

    with pytest.raises(CodexAgentHomeError, match="both native auth.json and custom"):
        _validate_codex_native_load(
            initialize=initialize,
            config_read=config_read,
            account_read=account_read,
            skills_list=skills,
            native_root=native,
            cwd=cwd,
            model_provider="deepseek",
            expected_session_flags=inputs.expected_session_flags,
        )
