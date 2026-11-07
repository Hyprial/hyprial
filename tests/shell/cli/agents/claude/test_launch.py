"""cli_claude_interactive: launch scenarios."""

from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from typing import Any
from typer.testing import CliRunner

from .support import (
    _initialized_command_home as _initialized_command_home,
    _legacy_runtime_context as _legacy_runtime_context,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import subprocess as _cli_subprocess
import pytest
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


@pytest.mark.parametrize("explicit,expected", [((), "auto"), (("--permission-mode", "plan"), "plan")])
def test_interactive_claude_uses_current_stored_args(monkeypatch: Any, tmp_path: Path, explicit: tuple[str, ...], expected: str) -> None:
    observed: dict[str, Any] = {}

    def request(method: str, params=None, **_kwargs):
        if method == "agent.create":
            return {"ok": True, "agent": {"harnessArgs": {"claude": ["--permission-mode", "auto"]}}}
        return {
            "daemon": {"running": True, "nodeId": "node-1", "owner": "op"},
            "interactiveSessions": [{"actor": "agent:op:node-1:worker", "sessionRef": observed.get("session"), "channelConfirmed": True}],
        }

    class Process:
        def __init__(self, argv, **_kwargs):
            observed["argv"] = argv
            observed["session"] = argv[argv.index("--session-id") + 1]
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    monkeypatch.setattr(_cli_subprocess, "Popen", Process)
    result = CliRunner().invoke(_cli_root_app, ["agent", "start", "claude", "--name", "worker", "--cwd", str(tmp_path), "--json", "--", *explicit])
    assert result.exit_code == 0, result.output
    assert observed["argv"].count("--permission-mode") == 1
    assert observed["argv"][observed["argv"].index("--permission-mode") + 1] == expected
    assert "Using stored harness_args" in result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_interactive_claude_rejects_invalid_stored_permission_mode(monkeypatch: Any, tmp_path: Path) -> None:
    def request(method: str, params=None, **_kwargs):
        if method == "agent.create":
            return {"ok": True, "agent": {"harnessArgs": {"claude": ["--permission-mode", "invalid-probe"]}}}
        assert method == "ps"
        return {"daemon": {"running": True, "nodeId": "node-1", "owner": "op"}}

    from hyprial.daemon.impl.mcp.channel import ownership as channel_ownership

    def forbidden_spawn(*_args, **_kwargs):
        raise AssertionError("invalid stored mode must not spawn Claude")

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    monkeypatch.setattr(channel_ownership, "_read_process_identity", lambda _pid: "isolated-birth")
    monkeypatch.setattr(_cli_subprocess, "Popen", forbidden_spawn)
    result = CliRunner().invoke(_cli_root_app, ["agent", "start", "claude", "--name", "worker", "--cwd", str(tmp_path), "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == "INVALID_ARGUMENT"
    assert "invalid Claude --permission-mode" in payload["error"]


def test_start_claude_interactive_golden_byte_exact(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """G1 golden: every observable output of _start_interactive_claude, byte-exact.

    This snapshot guards the coordinator's production path. Any change to the
    argv sequence, the on-disk mcp-config JSON, the settings JSON, or the
    registration polling parameters must fail here before it can reach a
    running coordinator. Deterministic uuid4 values make the snapshot stable.
    """

    import uuid as uuid_module

    from hyprial.daemon.impl.mcp.channel import ownership as mcp_channel

    state_dir = tmp_path / "state"
    hyprial_home = tmp_path / "home"
    session_uuid = uuid_module.UUID("11111111-2222-3333-4444-555555555555")
    config_uuid = uuid_module.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    uuids = iter([session_uuid, config_uuid])
    monkeypatch.setattr(_cli_get_services(), "uuid4", lambda: next(uuids))
    owner_identity = "test-owner-process-birth"
    # The Popen golden below replaces subprocess module-wide on macOS; pin
    # the already-measured birth marker so that replacement only captures the
    # Claude launch it is intended to inspect.
    monkeypatch.setattr(
        mcp_channel, "_read_process_identity", lambda _pid: owner_identity
    )

    daemon_calls: list[tuple[str, dict[str, Any], float]] = []
    observed: dict[str, Any] = {}

    def daemon_request(
        method: str, params: dict[str, Any] | None = None, *, timeout: float = 15.0
    ) -> dict[str, Any]:
        daemon_calls.append((method, params or {}, timeout))
        response: dict[str, Any] = {
            "daemon": {"running": True, "nodeId": "node-1", "owner": "op"}
        }
        if "session_ref" in observed:
            response["interactiveSessions"] = [
                {
                    "actor": "agent:op:node-1:worker-node",
                    "sessionRef": observed["session_ref"],
                    "channelConfirmed": True,
                }
            ]
        return response

    class Process:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            self.returncode: int | None = None
            observed["argv"] = list(argv)
            observed["kwargs"] = kwargs
            observed["session_ref"] = argv[argv.index("--session-id") + 1]
            config_path = Path(argv[argv.index("--mcp-config") + 1])
            observed["config_bytes"] = config_path.read_bytes()
            observed["config_path"] = config_path

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is None
            self.returncode = 0
            return 0

    monkeypatch.setenv("HYPRIAL_HOME", str(hyprial_home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HARNESS_CLAUDE_BIN", "/test/bin/claude")
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
            "--nickname",
            "night-shift",
            "--cwd",
            str(tmp_path),
            "--json",
            "--",
            "--model",
            "sonnet",
        ],
    )

    assert result.exit_code == 0, result.output

    session_ref = "11111111-2222-3333-4444-555555555555"
    config_path = state_dir / f"claude-channel-{session_ref}-aaaaaaaa.json"
    recovery_path = config_path.with_suffix(".recover")
    turn_signal_dir = config_path.with_suffix(".turns")
    owner_pid = os.getpid()

    # 1. argv sequence, byte-exact.
    recovery_hook = {
        "type": "command",
        "command": sys.executable,
        "args": [
            "-m",
            "hyprial.daemon.impl.mcp.entry",
            "claude-channel-recover",
            "--signal-path",
            str(recovery_path),
        ],
    }
    turn_hook = {
        "type": "command",
        "command": sys.executable,
        "args": [
            "-m",
            "hyprial.biz.impl.turn_pulse",
            "--signal-dir",
            str(turn_signal_dir),
        ],
        "timeout": 5,
    }
    start_hook = {
        "type": "command",
        "command": sys.executable,
        "args": [
            "-m",
            "hyprial.biz.impl.turn_pulse",
            "--signal-dir",
            str(turn_signal_dir / "start"),
        ],
        "timeout": 5,
    }
    settings_json = json.dumps(
        {
            "permissions": {
                "allow": [
                    "mcp__harness-bridge__harness_ack",
                    "mcp__harness-bridge__harness_progress",
                    "mcp__harness-bridge__harness_read",
                    "mcp__harness-bridge__harness_read_body",
                    "mcp__harness-bridge__harness_reply",
                    "mcp__harness-bridge__harness_send",
                    "mcp__harness-bridge__harness_targets",
                    "mcp__harness-bridge__harness_whoami",
                ]
            },
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup|resume|clear|compact",
                        "hooks": [recovery_hook],
                    }
                ],
                "UserPromptSubmit": [{"hooks": [start_hook]}],
                "Stop": [{"hooks": [turn_hook]}],
                "StopFailure": [
                    {
                        "matcher": (
                            "rate_limit|overloaded|authentication_failed|"
                            "oauth_org_not_allowed|billing_error|invalid_request|"
                            "model_not_found|server_error|max_output_tokens|unknown"
                        ),
                        "hooks": [recovery_hook],
                    }
                ],
            },
        },
        separators=(",", ":"),
    )
    expected_argv = [
        "/test/bin/claude",
        "--model",
        "sonnet",
        "--session-id",
        session_ref,
        "--name",
        "night-shift",
        "--mcp-config",
        str(config_path),
        "--strict-mcp-config",
        # The packaged hyprial skill rides a per-launch session-scoped plugin, so
        # a session's capability surface comes from HYPRIAL_HOME rather than from
        # whatever the launch directory happens to have registered.
        "--plugin-dir",
        str(state_dir / f"{config_path.stem}-skills"),
        "--settings",
        settings_json,
        "--dangerously-load-development-channels",
        "server:harness-bridge",
        "--append-system-prompt",
        (
            "Harness Network is connected through the harness-bridge MCP server. "
            "When a Harness channel notification arrives, immediately call "
            "harness_read and handle every pending message in FIFO order. Reply to "
            "requests with harness_reply; use harness_ack only when no reply is "
            "required. Never treat the channel notification itself as message body."
        ),
    ]
    assert observed["argv"] == expected_argv

    # 2. On-disk mcp-config JSON, byte-exact.
    expected_config = {
        "mcpServers": {
            "harness-bridge": {
                "type": "stdio",
                "command": sys.executable,
                "args": [
                    "-m",
                    "hyprial.daemon.impl.mcp.entry",
                    "claude-channel",
                    "--actor",
                    # The bare name only: the daemon mints the canonical URI
                    # per call from its current owner/node, so the launch
                    # never freezes a derived address into long-lived config
                    # (#352 owner-switch stranding).
                    "worker-node",
                    "--session-ref",
                    session_ref,
                    "--cwd",
                    str(tmp_path),
                    "--command",
                    "claude",
                    "--recovery-signal",
                    str(recovery_path),
                    "--turn-signal-dir",
                    str(turn_signal_dir),
                    "--owner-pid",
                    str(owner_pid),
                    "--owner-identity",
                    owner_identity,
                ],
                "env": {
                    "HYPRIAL_HOME": str(hyprial_home.resolve()),
                    "HARNESS_STATE_DIR": str(state_dir.resolve()),
                },
            }
        }
    }
    expected_config_bytes = (
        json.dumps(expected_config, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    assert observed["config_bytes"] == expected_config_bytes

    # 3. Spawn kwargs: cwd, whitelist-replacement environment (P1b B2 —
    # ambient inheritance is restricted to the frozen table's BASE+GENERATED
    # names; the provider mapping and the daemon-bound identity are explicit
    # deltas), stdout routed to stderr (CliRunner rebinds sys.stderr during
    # invoke, so compare by stream name).
    assert observed["kwargs"]["cwd"] == tmp_path
    import os as os_module

    from hyprial.identity.impl.agents.home.environment import whitelist_replacement_environment

    assert observed["kwargs"]["env"] == whitelist_replacement_environment(
        dict(os_module.environ),
        # M2 (PR #536 review): the attached carrier and every shell it runs
        # carry the daemon-bound identity, so `hyprial workflow` writes from the
        # TUI ride the fenced path instead of the human identity.
        {
            "HYPRIAL_AGENT_SESSION": "1",
            "HYPRIAL_WORKER_ACTOR": "agent:op:node-1:worker-node",
            "HYPRIAL_WORKER_SESSION_REF_FILE": str(
                config_path.with_suffix(".session-ref")
            ),
        },
    )
    assert observed["kwargs"]["stdout"] is not None
    assert observed["kwargs"]["stdout"].name == "<stderr>"

    # 4. Registration polling parameters: agent create first (decision A5 --
    # `hyprial agent start` creates the agent through the same command `hyprial agent
    # create` uses, so no connector can exist without an identity), then ps
    # for actor resolution, then one ps poll per 0.1s tick until
    # channelConfirmed, each with default timeout.
    assert daemon_calls == [
        (
            "agent.create",
            {
                "name": "worker-node",
                "existing": "reuse",
                "harness": "claude",
                "runtime": "interactive",
                "cwd": str(tmp_path.resolve()),
            },
            15.0,
        ),
        ("ps", {}, 15.0),
        ("ps", {}, 15.0),
    ]

    # 5. Result payload, byte-exact (wire keys unchanged).
    payload = json.loads(result.stdout)
    assert payload == {
        "ok": True,
        "actor": "agent:op:node-1:worker-node",
        "provider": "claude",
        "runtime": "interactive",
        "sessionRef": session_ref,
        "runtimeExitCode": 0,
    }
    # The per-launch config file is cleaned up.
    assert not observed["config_path"].exists()
    assert not recovery_path.exists()


def test_start_claude_interactive_injects_channel_mcp_and_settings(
    monkeypatch: Any, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    calls: list[tuple[str, dict[str, Any]]] = []
    observed: dict[str, Any] = {}

    def daemon_request(
        method: str, params: dict[str, Any] | None = None, *, timeout: float = 15.0
    ) -> dict[str, Any]:
        calls.append((method, params or {}))
        assert timeout > 0
        response: dict[str, Any] = {
            "daemon": {"running": True, "nodeId": "node-1", "owner": "op"}
        }
        if "session_ref" in observed:
            response["interactiveSessions"] = [
                {
                    "actor": "agent:op:node-1:worker-node",
                    "sessionRef": observed["session_ref"],
                    "channelConfirmed": True,
                }
            ]
        return response

    class Process:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            self.returncode: int | None = None
            observed["argv"] = argv
            observed["kwargs"] = kwargs
            observed["session_ref"] = argv[argv.index("--session-id") + 1]
            config_path = Path(argv[argv.index("--mcp-config") + 1])
            assert config_path.exists()
            assert config_path.stat().st_mode & 0o777 == 0o600
            observed["config_path"] = config_path
            observed["config"] = json.loads(config_path.read_text(encoding="utf-8"))

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is None
            self.returncode = 0
            return 0

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HARNESS_CLAUDE_BIN", "/test/bin/claude")
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
            "--nickname",
            "night-shift",
            "--cwd",
            str(tmp_path),
            "--json",
            "--",
            "--model",
            "sonnet",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # The CLI mints the canonical four-segment identity for the bare --name
    # so the registration matches pins and delivery addressing.
    assert payload == {
        "ok": True,
        "actor": "agent:op:node-1:worker-node",
        "provider": "claude",
        "runtime": "interactive",
        "sessionRef": payload["sessionRef"],
        "runtimeExitCode": 0,
    }
    argv = observed["argv"]
    assert argv[:3] == ["/test/bin/claude", "--model", "sonnet"]
    assert "--channels" not in argv
    assert argv.count("--strict-mcp-config") == 1
    assert argv.index("--strict-mcp-config") == argv.index("--mcp-config") + 2
    assert "--dangerously-load-development-channels" in argv
    assert (
        argv[argv.index("--dangerously-load-development-channels") + 1]
        == "server:harness-bridge"
    )
    assert argv[argv.index("--session-id") + 1] == payload["sessionRef"]
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert "mcp__harness-bridge__harness_read" in settings["permissions"]["allow"]
    assert "mcp__harness-bridge__harness_reply" in settings["permissions"]["allow"]
    assert settings["hooks"]["SessionStart"][0]["matcher"] == (
        "startup|resume|clear|compact"
    )
    assert settings["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 5
    assert settings["hooks"]["Stop"][0]["hooks"][0]["args"][:2] == [
        "-m",
        "hyprial.biz.impl.turn_pulse",
    ]
    assert settings["hooks"]["Stop"][0]["hooks"][0]["args"][-1].endswith(
        ".turns"
    )
    assert "rate_limit" in settings["hooks"]["StopFailure"][0]["matcher"]
    assert "model_not_found" in settings["hooks"]["StopFailure"][0]["matcher"]

    server = observed["config"]["mcpServers"]["harness-bridge"]
    assert server["type"] == "stdio"
    assert server["command"]
    assert server["args"][:3] == ["-m", "hyprial.daemon.impl.mcp.entry", "claude-channel"]
    # F1: the launch persists the BARE actor name; the daemon mints the
    # canonical URI per call, so an owner change never strands a running
    # channel on a dead owner segment (#352).
    assert server["args"][server["args"].index("--actor") + 1] == "worker-node"
    assert "agent:" not in json.dumps(observed["config"])
    assert (
        server["args"][server["args"].index("--session-ref") + 1]
        == payload["sessionRef"]
    )
    recovery_path = Path(server["args"][server["args"].index("--recovery-signal") + 1])
    for event in ("SessionStart", "StopFailure"):
        hook_args = settings["hooks"][event][0]["hooks"][0]["args"]
        assert Path(hook_args[hook_args.index("--signal-path") + 1]) == recovery_path
    assert server["env"] == {
        "HYPRIAL_HOME": str((tmp_path / "home").resolve()),
        "HARNESS_STATE_DIR": str(state_dir.resolve()),
    }
    # Decision A5: `hyprial agent start` reaches agent creation through `agent.create`
    # before it resolves the actor or launches anything.
    assert calls[0][0] == "agent.create"
    assert calls[0][1]["name"] == "worker-node"
    assert calls[0][1]["existing"] == "reuse"
    assert calls[0][1]["harness"] == "claude"
    assert calls[1:] == [("ps", {}), ("ps", {})]
    assert not observed["config_path"].exists()
