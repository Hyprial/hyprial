"""cli_claude_interactive: profile scenarios."""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any
import pytest
from typer.testing import CliRunner
from hyprial.daemon.impl.harnesses.model_provider import ModelProviderError

from .support import (
    _initialized_command_home as _initialized_command_home,
    _legacy_runtime_context as _legacy_runtime_context,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_start_claude_interactive_consumes_p2_profile_and_setting_slots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hyprial.daemon.impl.mcp import channel as mcp_channel

    state_dir = tmp_path / "state"
    profile = {
        "HOME": str(tmp_path / "tool-home"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "native-claude"),
        "HYPRIAL_TOOL_PROFILE_ID": "p23-tui",
        "HYPRIAL_TEA_LOGIN": "code.hyprial.com",
    }
    observed: dict[str, Any] = {}

    def daemon_request(
        method: str, params: dict[str, Any] | None = None, *, timeout: float = 15.0
    ) -> dict[str, Any]:
        assert timeout > 0
        response: dict[str, Any] = {
            "daemon": {"running": True, "nodeId": "node-1", "owner": "op"}
        }
        if method == "ps" and "session_ref" in observed:
            response["interactiveSessions"] = [
                {
                    "actor": "agent:op:node-1:worker-node",
                    "sessionRef": observed["session_ref"],
                    "channelConfirmed": True,
                }
            ]
        return response

    class Process:
        @classmethod
        def __class_getitem__(cls, _item: object) -> type[Process]:
            return cls

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            self.returncode: int | None = None
            observed["argv"] = list(argv)
            observed["env"] = dict(kwargs["env"])
            observed["session_ref"] = argv[argv.index("--session-id") + 1]

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is None
            self.returncode = 0
            return 0

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HARNESS_CLAUDE_BIN", "/test/bin/claude")
    monkeypatch.setenv("HOME", "/ambient/home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/ambient/claude")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/ambient/agent.sock")
    credentials = Path(profile["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
    credentials.parent.mkdir()
    credentials.write_text("P23_TUI_FILE_NOT_A_SECRET\n", encoding="utf-8")
    credentials.chmod(0o600)
    monkeypatch.setattr(
        _cli_get_services(), "_runtime_context_environment", lambda **_kwargs: dict(profile)
    )
    monkeypatch.setattr(mcp_channel, "_read_process_identity", lambda _pid: None)
    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr(_cli_subprocess, "Popen", Process)

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "claude",
            "--name",
            "worker-node",
            "--cwd",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    argv = observed["argv"]
    assert argv[argv.index("--setting-sources") + 1] == "user,project,local"
    assert argv.count("--strict-mcp-config") == 1
    environment = observed["env"]
    assert environment["HOME"] == profile["HOME"]
    assert environment["CLAUDE_CONFIG_DIR"] == profile["CLAUDE_CONFIG_DIR"]
    assert environment["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert environment["HYPRIAL_TOOL_PROFILE_ID"] == "p23-tui"
    assert "SSH_AUTH_SOCK" not in environment


def test_start_claude_interactive_reports_missing_p2_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native_root = tmp_path / "native-claude"
    native_root.mkdir()
    profile = {
        "HOME": str(tmp_path / "tool-home"),
        "CLAUDE_CONFIG_DIR": str(native_root),
    }
    secret = "P23_TUI_ERROR_MUST_NOT_PRINT_THIS_SECRET"
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_OAUTH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UNRELATED_SECRET", secret)
    monkeypatch.setattr(_cli_get_services(), "_create_agent_for_start", lambda **_kwargs: {})
    monkeypatch.setattr(
        _cli_get_services(), "_runtime_context_environment", lambda **_kwargs: dict(profile)
    )
    calls: list[str] = []
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, *_args, **_kwargs: calls.append(method)
        or {"daemon": {"nodeId": "node", "owner": "owner"}},
    )

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "claude",
            "--name",
            "worker-node",
            "--cwd",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "CLAUDE_CREDENTIAL_MISSING"
    assert str(native_root / ".credentials.json") in payload["error"]
    assert "ANTHROPIC_API_KEY" in payload["error"]
    assert secret not in result.output
    assert calls == ["ps"]


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("tmux", [False, True])
def test_workflow_legacy_cli_checks_own_source_before_spawn(monkeypatch, tmp_path, native, tmux):
    home = tmp_path / "agent"
    home.mkdir(mode=0o700)
    if native:
        root = home / "secrets/native/claude"
        root.mkdir(parents=True, mode=0o700)
        root.parent.chmod(0o700)
        root.parent.parent.chmod(0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "hyprial"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_get_services(), "_runtime_context_environment", lambda **kw: None)
    monkeypatch.setattr(_cli_get_services(), "_create_agent_for_start", lambda **kw: {
        "agent": {"capabilities": {"creationOrigin": "managed-launch"}}})
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda method, *a, **kw: (
        {"home": {"path": str(home)}} if method == "agent.ensure-home"
        else {"daemon": {"nodeId": "node", "owner": "owner"}}))
    spawned = []
    def spawn(*args, **kwargs):
        spawned.append(kwargs["env"])
        raise RuntimeError("synthetic spawn reached")
    monkeypatch.setattr(_cli_subprocess, "Popen", spawn)
    monkeypatch.setattr("hyprial.shell.impl.cli.commands.agent.runtime.claude._launch_detached_tui", spawn)
    result = CliRunner().invoke(_cli_root_app, ["agent", "start", "claude", "--name", "worker",
                                        "--cwd", str(tmp_path), "--json", *(["--tmux"] if tmux else [])])
    if native:
        assert spawned and spawned[0]["CLAUDE_CONFIG_DIR"] == str(root)
    else:
        assert not spawned
        assert json.loads(result.stdout)["code"] == "CLAUDE_CREDENTIAL_MISSING"


def test_start_claude_interactive_p2_refuses_legacy_home_deepseek_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tool_home = tmp_path / "tool-home"
    legacy_auth = tool_home / ".pi" / "agent" / "auth.json"
    legacy_auth.parent.mkdir(parents=True)
    legacy_auth.write_text(
        json.dumps({"deepseek": {"type": "api_key", "key": "legacy-bait"}}),
        encoding="utf-8",
    )
    profile = {
        "HOME": str(tool_home),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "native-claude"),
    }
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(_cli_get_services(), "_create_agent_for_start", lambda **_kwargs: {})
    monkeypatch.setattr(
        _cli_get_services(), "_runtime_context_environment", lambda **_kwargs: dict(profile)
    )
    calls: list[str] = []
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, *_args, **_kwargs: calls.append(method)
        or {"daemon": {"nodeId": "node", "owner": "owner"}},
    )

    with pytest.raises(ModelProviderError, match="agent-owned DEEPSEEK_API_KEY"):
        _cli_get_services()._start_interactive_claude(
            name="worker-node",
            nickname=None,
            cwd=tmp_path,
            resume=None,
            runtime_args=(),
            model_provider="deepseek",
            model="deepseek-reasoner",
            json_output=True,
        )
    assert calls == ["ps"]
