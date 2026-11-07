"""cli_claude_interactive: hooks scenarios."""

from __future__ import annotations
import os
import sys
from pathlib import Path

from typer.testing import CliRunner

from hyprial.daemon.impl.mcp import entry as mcp_entry

from .support import (
    _initialized_command_home as _initialized_command_home,
    _legacy_runtime_context as _legacy_runtime_context,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import subprocess as _cli_subprocess
from hyprial.shell.impl.cli.commands.common import support as _cli_support
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_claude_recovery_hook_entry_coalesces_atomic_signal(tmp_path: Path) -> None:
    signal_path = tmp_path / "session.recover"

    first = mcp_entry.main(
        ["claude-channel-recover", "--signal-path", str(signal_path)]
    )
    second = mcp_entry.main(
        ["claude-channel-recover", "--signal-path", str(signal_path)]
    )

    assert first == 0
    assert second == 0
    assert signal_path.is_file()
    assert signal_path.stat().st_mode & 0o777 == 0o600


def test_stop_hook_entry_point_is_stdlib_only_silent_and_never_fails(
    tmp_path: Path,
) -> None:
    """The Stop hook runs the real module in a fresh interpreter: it must not
    import hyprial.cli / hyprial.mcp (about 1 s on a loaded host, long enough
    for Claude Code's hook timeout to drop the pulse)."""

    import subprocess

    import hyprial

    # The src root, so the fresh interpreter imports this checkout.
    env = {**os.environ, "PYTHONPATH": str(Path(hyprial.__file__).resolve().parents[1])}
    signal_dir = tmp_path / "session.turns"
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, runpy; sys.argv=['turn_pulse','--signal-dir',sys.argv[1]];"
            "\ntry:\n runpy.run_module('hyprial.biz.impl.turn_pulse', run_name='__main__')"
            "\nexcept SystemExit as e:\n assert e.code == 0, e.code"
            "\nheavy=[m for m in sys.modules if m.startswith(('hyprial.cli','hyprial.mcp','hyprial.daemon'))]"
            "\nassert not heavy, heavy",
            str(signal_dir),
        ],
        env=env,
        input='{"hook_event_name":"Stop"}\n',
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout == ""
    assert len(tuple(signal_dir.iterdir())) == 1

    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied", encoding="utf-8")
    failed = subprocess.run(
        [sys.executable, "-m", "hyprial.biz.impl.turn_pulse", "--signal-dir", str(blocked)],
        env=env,
        input='{"hook_event_name":"Stop"}\n',
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert failed.returncode == 0, failed.stderr
    assert failed.stdout == "" and failed.stderr == ""


def test_user_prompt_submit_writes_a_start_pulse(tmp_path: Path) -> None:
    """L5: the session settings carry a UserPromptSubmit hook that runs the
    same stdlib-only pulse module against ``<turns>/start`` (wp5c.md §4.1)."""

    import json
    from typing import Any

    import pytest

    mp = pytest.MonkeyPatch()
    state_dir = tmp_path / "state"
    observed: dict[str, Any] = {}

    def daemon_request(
        method: str, params: dict[str, Any] | None = None, *, timeout: float = 15.0
    ) -> dict[str, Any]:
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
            observed["session_ref"] = argv[argv.index("--session-id") + 1]

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = 0
            return 0

    try:
        mp.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
        mp.setenv("HARNESS_STATE_DIR", str(state_dir))
        mp.setenv("HARNESS_CLAUDE_BIN", "/test/bin/claude")
        mp.setattr(_cli_support, "_daemon_request", daemon_request)
        mp.setattr(_cli_subprocess, "Popen", Process)
        result = CliRunner().invoke(
            _cli_root_app,
            ["agent", "start", "claude", "--name", "worker-node", "--cwd", str(tmp_path), "--json"],
        )
    finally:
        mp.undo()

    assert result.exit_code == 0, result.output
    argv = observed["argv"]
    settings = json.loads(argv[argv.index("--settings") + 1])
    hooks = settings["hooks"]
    start = hooks["UserPromptSubmit"][0]["hooks"][0]
    stop = hooks["Stop"][0]["hooks"][0]
    assert start["args"][:2] == ["-m", "hyprial.biz.impl.turn_pulse"]
    assert start["args"][2] == "--signal-dir"
    assert start["args"][3] == str(Path(stop["args"][3]) / "start")
    assert start["timeout"] == 5
    # SessionStart / compact are not model turns: they never write a start pulse.
    assert "turn_pulse" not in json.dumps(hooks["SessionStart"])
