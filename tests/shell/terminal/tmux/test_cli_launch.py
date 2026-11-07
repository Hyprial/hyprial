"""tui_tmux: cli launch scenarios."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications
import json
from pathlib import Path
from typing import Any
from typer.testing import CliRunner
from hyprial.daemon.impl.harnesses.runtime._launch_cleanup import skill_plugin_path
from hyprial.daemon.impl.harnesses.runtime import tmux as tmux_mod

from .support import (
    _install_fake_daemon,
    _install_fake_tmux,
    _invoke_pi_tmux_start,
    _invoke_tmux_start,
    _legacy_runtime_context as _legacy_runtime_context,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_start_claude_tmux_spawns_detached_and_returns_attach_hints(
    monkeypatch: Any, tmp_path: Path
) -> None:
    observed: dict[str, Any] = {}
    _install_fake_daemon(monkeypatch, observed)
    kill_record = _install_fake_tmux(monkeypatch, observed)

    result = _invoke_tmux_start(monkeypatch, tmp_path)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["detached"] is True
    assert (
        payload["tmuxSession"]
        == observed["tmux_session_name"]
        == "hyprial-agent-op-node-1-worker-node"
    )
    assert payload["attach"] == {
        "macOS-iTerm2": "tmux -CC attach -t hyprial-agent-op-node-1-worker-node",
        "wsl-linux-terminal": "tmux attach -t hyprial-agent-op-node-1-worker-node",
    }
    assert payload["sessionRef"] == "11111111-2222-3333-4444-555555555555"
    # The detached path never touches the foreground Popen wait.
    assert "runtimeExitCode" not in payload
    assert kill_record["killed"] == []

    config = json.loads(observed["config_bytes"].decode())
    server_args = config["mcpServers"]["harness-bridge"]["args"]
    # The carrier learns the tmux session name (daemon record + pane fence)...
    assert "--tmux-session" in server_args
    assert (
        server_args[server_args.index("--tmux-session") + 1]
        == "hyprial-agent-op-node-1-worker-node"
    )
    # ...and the exiting launcher is never passed as the owner fence.
    assert "--owner-pid" not in server_args
    # The launch-scoped config stays on disk for in-session MCP reconnects.
    assert observed["config_path"].is_file()
    plugin_path = skill_plugin_path(observed["config_path"])
    assert plugin_path.is_dir()
    # Success has a real release owner before the detached launcher returns.
    [(owner_session, owner_config, owner_recovery)] = kill_record["cleanupOwners"]
    assert owner_session == "hyprial-agent-op-node-1-worker-node"
    assert owner_config == observed["config_path"]
    assert owner_recovery == observed["config_path"].with_suffix(".recover")

    # This unit test proves custody is handed to the pane wrapper. The
    # real-tmux test owns the actual release assertion.


def test_start_claude_tmux_reports_every_skipped_plugin(
    monkeypatch: Any, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    plugin_store = home / "plugins"
    plugin_store.mkdir(parents=True)
    extension = plugin_store / "pi-only.ts"
    extension.write_text("// pi only", encoding="utf-8")
    (plugin_store / "plugins.json").write_text(
        json.dumps(
            {
                "version": 1,
                "plugins": [
                    {
                        "name": "pi-only",
                        "kind": "pi-extension",
                        "harnesses": ["claude"],
                        "path": "pi-only.ts",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    observed: dict[str, Any] = {}
    _install_fake_daemon(monkeypatch, observed)
    ownership = _install_fake_tmux(monkeypatch, observed)

    result = _invoke_tmux_start(monkeypatch, tmp_path)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["warnings"] == [
        {
            "code": "PLUGIN_SKIPPED",
            "message": (
                "plugin 'pi-only' was not loaded: claude channel plugin "
                "injection cannot load kind 'pi-extension'"
            ),
            "data": {
                "plugin": "pi-only",
                "reason": (
                    "claude channel plugin injection cannot load kind "
                    "'pi-extension'"
                ),
            },
        }
    ]
    assert "plugin 'pi-only' was not loaded" in result.stderr
    assert len(ownership["cleanupOwners"]) == 1


def test_start_claude_tmux_owner_spawn_failure_cleans_before_registration(
    monkeypatch: Any, tmp_path: Path
) -> None:
    observed: dict[str, Any] = {}
    _install_fake_daemon(monkeypatch, observed)
    ownership = _install_fake_tmux(
        monkeypatch, observed, spawn_fails=True
    )

    result = _invoke_tmux_start(monkeypatch, tmp_path)

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "TMUX_SPAWN_FAILED"
    assert "spawned" not in observed
    assert ownership["killed"] == []
    assert not observed["config_path"].exists()
    assert not skill_plugin_path(observed["config_path"]).exists()


def test_start_claude_tmux_dead_session_before_registration_fails_and_cleans_up(
    monkeypatch: Any, tmp_path: Path
) -> None:
    observed: dict[str, Any] = {}
    _install_fake_daemon(monkeypatch, observed, registers=False)
    kill_record = _install_fake_tmux(monkeypatch, observed, session_dies=True)

    result = _invoke_tmux_start(monkeypatch, tmp_path)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["code"] == "CHANNEL_REGISTRATION_FAILED"
    assert kill_record["killed"] == ["hyprial-agent-op-node-1-worker-node"]
    assert not observed["config_path"].exists()
    assert not skill_plugin_path(observed["config_path"]).exists()


def test_start_claude_tmux_requires_tmux_binary(
    monkeypatch: Any, tmp_path: Path
) -> None:
    observed: dict[str, Any] = {}
    _install_fake_daemon(monkeypatch, observed)
    monkeypatch.setattr(tmux_mod, "find_tmux", lambda: None)

    result = _invoke_tmux_start(monkeypatch, tmp_path)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "TMUX_UNAVAILABLE"
    assert "spawned" not in observed
    assert [
        item for item in (tmp_path / "state").iterdir() if item.name != "logs"
    ] == []


def test_start_claude_tmux_refuses_to_clobber_existing_session(
    monkeypatch: Any, tmp_path: Path
) -> None:
    observed: dict[str, Any] = {}
    _install_fake_daemon(monkeypatch, observed)
    monkeypatch.setattr(tmux_mod, "find_tmux", lambda: "/fake/tmux")
    monkeypatch.setattr(tmux_mod, "has_session", lambda _bin, _name: True)

    result = _invoke_tmux_start(monkeypatch, tmp_path)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "TMUX_SESSION_EXISTS"
    assert "tmux -CC attach -t hyprial-agent-op-node-1-worker-node" in str(
        payload["error"]
    )
    assert "spawned" not in observed
    assert [
        item for item in (tmp_path / "state").iterdir() if item.name != "logs"
    ] == []


def test_start_tmux_headless_combination_rejected(tmp_path: Path) -> None:
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
            "--tmux",
            "--headless",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "INVALID_ARGUMENT"


def test_start_tmux_rejected_for_harness_without_interactive_attach(
    tmp_path: Path,
) -> None:
    # dsh has no interactive attach, so the tmux story cannot ride on it; this
    # must not be silently stacked on a pty fallback.  (codex gained a
    # detached --tmux form in S5 and is covered by test_codex_tmux.py.)
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent",
            "start",
            "dsh",
            "--name",
            "worker-node",
            "--cwd",
            str(tmp_path),
            "--tmux",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "UNSUPPORTED_CAPABILITY"


def test_start_pi_tmux_spawns_detached_teaches_carrier_the_session_name(
    monkeypatch: Any, tmp_path: Path
) -> None:
    observed: dict[str, Any] = {}
    _install_fake_daemon(monkeypatch, observed, source="pi-extension")
    kill_record = _install_fake_tmux(monkeypatch, observed, capture_config=False)

    result = _invoke_pi_tmux_start(monkeypatch, tmp_path)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["provider"] == "pi"
    assert payload["detached"] is True
    assert (
        payload["tmuxSession"]
        == observed["tmux_session_name"]
        == "hyprial-agent-op-node-1-worker-node"
    )
    assert payload["attach"] == {
        "macOS-iTerm2": "tmux -CC attach -t hyprial-agent-op-node-1-worker-node",
        "wsl-linux-terminal": "tmux attach -t hyprial-agent-op-node-1-worker-node",
    }
    assert "runtimeExitCode" not in payload
    assert kill_record["killed"] == []

    # The carrier learns the session name through the injected environment;
    # the extension's session.register then carries tmuxSession to the daemon
    # (pinned in tests/test_pi_harness_attach_extension.py).
    env = observed["env"]
    assert env["HYPRIAL_WORKER_TMUX_SESSION"] == "hyprial-agent-op-node-1-worker-node"
    assert env["HYPRIAL_WORKER_ACTOR"] == "agent:op:node-1:worker-node"
    binding = Path(env["HYPRIAL_WORKER_SESSION_REF_FILE"])
    assert binding.is_absolute() and binding.is_file()
    assert binding.read_text() == "11111111-2222-3333-4444-555555555555"
    assert binding.stat().st_mode & 0o777 == 0o600
    assert env["HYPRIAL_AGENT_SESSION"] == "1"
    assert "HYPRIAL_WORKER_SESSION_REF" not in env
    assert "HYPRIAL_MANAGED_WORKER" not in env
    argv = observed["argv"]
    assert argv[0] == "/test/bin/pi"
    assert "--extension" in argv
    assert argv[argv.index("--session-id") + 1] == (
        "11111111-2222-3333-4444-555555555555"
    )


def test_start_pi_tmux_dead_session_before_registration_fails_and_cleans_up(
    monkeypatch: Any, tmp_path: Path
) -> None:
    observed: dict[str, Any] = {}
    _install_fake_daemon(monkeypatch, observed, registers=False)
    kill_record = _install_fake_tmux(
        monkeypatch, observed, session_dies=True, capture_config=False
    )

    result = _invoke_pi_tmux_start(monkeypatch, tmp_path)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "CHANNEL_REGISTRATION_FAILED"
    # The failure message keeps the pi trust-prompt diagnosis.
    assert "project-trust" in payload["error"]
    assert kill_record["killed"] == ["hyprial-agent-op-node-1-worker-node"]


def test_start_pi_tmux_refuses_to_clobber_existing_session(
    monkeypatch: Any, tmp_path: Path
) -> None:
    observed: dict[str, Any] = {}
    _install_fake_daemon(monkeypatch, observed)
    monkeypatch.setattr(tmux_mod, "find_tmux", lambda: "/fake/tmux")
    monkeypatch.setattr(tmux_mod, "has_session", lambda _bin, _name: True)

    result = _invoke_pi_tmux_start(monkeypatch, tmp_path)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "TMUX_SESSION_EXISTS"
    assert "spawned" not in observed
