"""cli_claude_interactive: recovery scenarios."""

from __future__ import annotations
import json
import shutil
import signal
from pathlib import Path
from typing import Any
import pytest
from typer.testing import CliRunner

from .support import (
    _initialized_command_home as _initialized_command_home,
    _legacy_runtime_context as _legacy_runtime_context,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_start_claude_interactive_fails_if_channel_never_registers(
    monkeypatch: Any, tmp_path: Path
) -> None:
    class Process:
        def __init__(self, _argv: list[str], **_kwargs: Any) -> None:
            pass

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            raise AssertionError("already-exited process must not be terminated")

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_args, **_kwargs: {"daemon": {"running": True}},
    )
    monkeypatch.setattr(_cli_subprocess, "Popen", Process)

    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "claude", "--name", "worker", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "CHANNEL_REGISTRATION_FAILED"


def test_start_claude_interactive_preserves_spawn_failure(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_args, **_kwargs: {"daemon": {"running": True}},
    )

    def missing(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError("claude executable missing")

    monkeypatch.setattr(_cli_subprocess, "Popen", missing)

    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "claude", "--name", "worker", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "claude executable missing"


def test_start_claude_interactive_resume_reuses_target_session(
    monkeypatch: Any, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    claude_root = tmp_path / "claude"
    resume_id = "11111111-2222-3333-4444-555555555555"
    from hyprial.daemon.impl.transfer.archive.session_files import (
        claude_project_dir_name,
    )

    transcript = (
        claude_root
        / "projects"
        / claude_project_dir_name(str(tmp_path))
        / f"{resume_id}.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    observed: dict[str, Any] = {}

    def daemon_request(
        method: str, params: dict[str, Any] | None = None, *, timeout: float = 15.0
    ) -> dict[str, Any]:
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
            observed["session_ref"] = argv[argv.index("--resume") + 1]
            config_path = Path(argv[argv.index("--mcp-config") + 1])
            assert config_path.exists()
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
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_root))
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
            "--resume",
            resume_id,
            "--json",
            "--",
            "--model",
            "sonnet",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    # Resume reuses the target session as the Harness ref: one identity.
    assert payload["sessionRef"] == resume_id
    argv = observed["argv"]
    assert argv[:3] == ["/test/bin/claude", "--model", "sonnet"]
    # The session id arrives exactly once, as --resume; hyprial must not also
    # inject --session-id, which Claude rejects as conflicting.
    assert "--session-id" not in argv
    assert "--fork-session" not in argv
    assert argv[argv.index("--resume") + 1] == resume_id
    server = observed["config"]["mcpServers"]["harness-bridge"]
    assert server["args"][server["args"].index("--session-ref") + 1] == resume_id


def test_start_claude_missing_resume_does_not_write_agent(
    monkeypatch: Any, tmp_path: Path
) -> None:
    calls: list[str] = []

    def daemon_request(method: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        calls.append(method)
        if method == "agent.runtime-context":
            return {"ok": True, "mode": "legacy", "environment": {}}
        raise AssertionError(method)

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
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
            "--resume",
            "missing-native-session",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "RESUME_SESSION_NOT_FOUND"
    assert "agent.create" not in calls


@pytest.mark.parametrize(
    "flag",
    [
        "--session-id",
        "--session-id=abc",
        "--resume",
        "--resume=abc",
        "--continue",
        "-c",
        "--fork-session",
    ],
)
def test_start_claude_interactive_rejects_session_identity_runtime_args(
    monkeypatch: Any, tmp_path: Path, flag: str
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("rejected args must never reach the daemon or spawn")

    monkeypatch.setattr(_cli_support, "_daemon_request", forbidden)
    monkeypatch.setattr(_cli_subprocess, "Popen", forbidden)

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "claude",
            "--name",
            "worker",
            "--cwd",
            str(tmp_path),
            "--json",
            "--",
            flag,
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "INVALID_ARGUMENT"
    assert "--resume <session-id>" in payload["error"]


def test_start_resume_rejected_where_unsupported(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Unsupported runtimes explain the native reason before daemon access."""

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("rejected args must never reach the daemon")

    monkeypatch.setattr(_cli_support, "_daemon_request", forbidden)

    headless = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "dsh",
            "--name",
            "worker",
            "--cwd",
            str(tmp_path),
            "--headless",
            "--resume",
            "abc",
            "--json",
        ],
    )
    assert headless.exit_code == 1
    headless_payload = json.loads(headless.stdout)
    assert headless_payload["code"] == "INVALID_ARGUMENT"
    assert "fresh worker MCP identity" in headless_payload["error"]

    jev = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "jev",
            "--name",
            "worker",
            "--cwd",
            str(tmp_path),
            "--resume",
            "abc",
            "--json",
        ],
    )
    assert jev.exit_code == 1
    assert "no persistent session" in json.loads(jev.stdout)["error"]

    user_proxy = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "user-proxy",
            "--name",
            "worker",
            "--cwd",
            str(tmp_path),
            "--resume",
            "abc",
            "--json",
            "--",
            "--route",
            "route:lark:test",
        ],
    )
    assert user_proxy.exit_code == 1
    assert "stateless relay" in json.loads(user_proxy.stdout)["error"]


def test_start_help_documents_resume_support_and_precise_refusals() -> None:
    import click

    result = CliRunner().invoke(_cli_root_app, ["agent", "start", "--help"])
    assert result.exit_code == 0, result.output
    text = " ".join(click.unstyle(result.output).replace("│", " ").split())
    assert "interactive claude, pi, and codex" in text
    assert "fresh worker MCP identity" in text
    assert "jev has no persistent session" in text
    assert "user-proxy is a stateless relay" in text


def test_start_claude_interactive_leaves_sigint_to_the_tui(
    monkeypatch: Any, tmp_path: Path
) -> None:
    observed: dict[str, Any] = {}

    def daemon_request(
        method: str, params: dict[str, Any] | None = None, *, timeout: float = 15.0
    ) -> dict[str, Any]:
        assert timeout > 0
        response: dict[str, Any] = {
            "daemon": {"running": True, "nodeId": "node-1", "owner": "op"}
        }
        if "session_ref" in observed:
            response["interactiveSessions"] = [
                {
                    "actor": "agent:op:node-1:worker",
                    "sessionRef": observed["session_ref"],
                    "channelConfirmed": True,
                }
            ]
        return response

    class Process:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            self.returncode: int | None = None
            observed["session_ref"] = argv[argv.index("--session-id") + 1]

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is None
            # While the TUI owns the terminal, Ctrl-C (SIGINT) must be left to
            # Claude: the hyprial supervisor ignores it instead of interrupting
            # wait() and tearing the session down.
            observed["sigint_during_tui"] = signal.getsignal(signal.SIGINT)
            self.returncode = 0
            return 0

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr(_cli_subprocess, "Popen", Process)

    previous = signal.getsignal(signal.SIGINT)
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "claude", "--name", "worker", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert observed["sigint_during_tui"] == signal.SIG_IGN
    assert signal.getsignal(signal.SIGINT) == previous


@pytest.mark.parametrize("harness", ["codex"])
def test_start_codex_non_headless_routes_to_app_server_attach(
    monkeypatch: Any, tmp_path: Path, harness: str
) -> None:
    """Codex interactive now routes through the PR2 app-server launcher."""

    called: list[dict[str, Any]] = []

    monkeypatch.setattr(
        _cli_get_services(),
        "_start_interactive_codex",
        lambda **kwargs: called.append(kwargs) or {"ok": True},
    )

    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", harness, "--name", "worker", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"ok": True}
    assert called == [
        {
            "name": "worker",
                "nickname": None,
                "cwd": tmp_path.resolve(),
                "resume": None,
                "runtime_args": (),
            "model_provider": None,
            "model": None,
            "json_output": True,
            "tmux": False,
        }
    ]


def test_start_claude_headless_remains_daemon_managed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def daemon_request(
        method: str, params: dict[str, Any] | None = None, *, timeout: float = 15.0
    ) -> dict[str, Any]:
        calls.append((method, params or {}))
        assert timeout > 0
        return {"ok": True, "id": "claude:batch"}

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    # Deterministic command pinning (task D): the registration resolves the
    # harness binary to an absolute path at `hyprial agent start` time.
    monkeypatch.setattr(shutil, "which", lambda name: f"/fake/bin/{name}")
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "claude",
            "--name",
            "batch",
            "--cwd",
            str(tmp_path),
            "--headless",
            "--json",
            "--",
            "--model",
            "haiku",
        ],
    )

    assert result.exit_code == 0, result.output
    # Decision A5: the headless path creates the agent through agent.create
    # too, so both entry points share one identity owner. The lifecycle.start
    # payload itself is unchanged (wire keys, "provider" included).
    assert calls == [
        (
            "agent.create",
            {
                "name": "batch",
                "existing": "reuse",
                "harness": "claude",
                "runtime": "headless",
                "cwd": str(tmp_path.resolve()),
            },
        ),
        (
            "lifecycle.start",
            {
                "provider": "claude",
                "name": "batch",
                "headless": True,
                "args": ["--model", "haiku"],
                "cwd": str(tmp_path.resolve()),
                "command": ["/fake/bin/claude"],
            },
        ),
    ]
