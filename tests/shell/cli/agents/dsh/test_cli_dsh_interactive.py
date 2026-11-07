"""CLI orchestration contract for `hyprial agent start dsh` interactive attach."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.agent.runtime.dsh import launch as dsh_launch
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

SESSION_REF = "11111111-2222-3333-4444-555555555555"
LAUNCH_SUFFIX = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ACTOR = "agent:op:node-1:dsh-tui"
# The launch rig replaces subprocess.Popen; the argv probe needs the real one.
_REAL_POPEN = subprocess.Popen


def _leftover_launch_dirs(tmp_path: Path) -> list[Path]:
    return list((tmp_path / "state").glob("dsh-tui-*"))


@pytest.fixture()
def launch_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    state_dir = tmp_path / "state"
    hyprial_home = tmp_path / "home"
    hyprial_home.mkdir(mode=0o700)
    fake_bin = tmp_path / "bin" / "dsh-tui"
    fake_bin.parent.mkdir()
    fake_bin.write_text("")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("HYPRIAL_HOME", str(hyprial_home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HARNESS_DSH_TUI_BIN", str(fake_bin))
    ids = iter((UUID(SESSION_REF), UUID(LAUNCH_SUFFIX)))
    monkeypatch.setattr(_cli_get_services(), "uuid4", lambda: next(ids))

    rig: dict[str, Any] = {
        "daemon_calls": [],
        "observed": {},
        "register": True,
        "tui_spawn_error": None,
        "carrier_spawn_error": None,
        "tui_poll_exit": None,
        "carrier_poll_exit": None,
        "tui_wait_code": 0,
        "carrier_wait_code": 0,
        "carrier_crash_after_registration": None,
        "carrier_stderr": None,
        "session_source": "dsh-channel",
        "session_process_pid": None,
        "dsh_session_id": None,
        "tui_exit_code": None,
        "tui_pid": None,
        "tui_identity": None,
        "terminated_tuis": [],
        "processes": [],
    }

    def daemon_request(
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 15.0,
        restore_wait: float | None = None,
    ) -> dict[str, Any]:
        rig["daemon_calls"].append((method, params or {}))
        response: dict[str, Any] = {
            "daemon": {"running": True, "nodeId": "node-1", "owner": "op"}
        }
        if method == "agent.create":
            response["agent"] = {"name": (params or {}).get("name")}
        if (
            method == "ps"
            and rig["register"]
            and rig["tui_spawn_error"] is None
            and rig["carrier_poll_exit"] is None
            and rig["observed"].get("carrier_argv") is not None
        ):
            env = rig["observed"]["tui_env"]
            rig["observed"]["registered"] = True
            record = {
                "actor": env["HYPRIAL_WORKER_ACTOR"],
                "sessionRef": env["HYPRIAL_WORKER_SESSION_REF"],
                "source": rig["session_source"],
            }
            if rig["session_process_pid"] == "tui":
                record["processPid"] = rig["tui_pid"]
            elif rig["session_process_pid"] is not None:
                record["processPid"] = rig["session_process_pid"]
            response["interactiveSessions"] = [record]
        return response

    class _FakeTui:
        """Stand-in for the TUI the carrier owns (the launcher never spawns it)."""

        is_carrier = False

        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode: int | None = None
            self.terminated = False

        def poll(self) -> int | None:
            return self.returncode

    class Process:
        next_pid = 12000

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            self.argv = list(argv)
            self.kwargs = kwargs
            assert "dsh-carrier" in self.argv, "the launcher must only spawn the carrier"
            self.is_carrier = True
            if rig["carrier_spawn_error"] is not None:
                raise rig["carrier_spawn_error"]
            self.pid = Process.next_pid
            Process.next_pid += 1
            self.returncode: int | None = None
            self.terminated = False
            rig["processes"].append(self)
            rig["observed"]["carrier_argv"] = list(argv)
            rig["observed"]["carrier_env"] = kwargs["env"]
            rig["observed"]["tui_env"] = kwargs["env"]
            launch_dir = Path(argv[argv.index("--launch-dir") + 1])
            rig["observed"]["launch_dir"] = launch_dir
            tui_argv = json.loads(argv[argv.index("--tui-argv") + 1])
            rig["observed"]["tui_argv"] = tui_argv
            patch_path = Path(tui_argv[tui_argv.index("--patch") + 1])
            rig["observed"]["patch_path"] = patch_path
            rig["observed"]["launch_dir_mode"] = launch_dir.stat().st_mode & 0o777
            rig["observed"]["patch"] = yaml.safe_load(
                patch_path.read_text(encoding="utf-8")
            )
            rows = rig["observed"]["patch"][0]["insert"]
            plugin_path = Path(rows[1]["name"])
            rig["observed"]["plugin_path"] = plugin_path
            rig["observed"]["plugin_mode"] = plugin_path.stat().st_mode & 0o777
            rig["observed"]["plugin_source"] = plugin_path.read_text(encoding="utf-8")
            rig["observed"]["control_socket"] = Path(
                rows[1]["config"]["socketPath"]
            )
            if rig["carrier_stderr"] is not None:
                (launch_dir / "carrier.stderr").write_text(
                    rig["carrier_stderr"], encoding="utf-8"
                )
            if rig["dsh_session_id"] is not None:
                (launch_dir / "dsh-session-id").write_text(
                    rig["dsh_session_id"], encoding="utf-8"
                )
            if rig["tui_spawn_error"] is not None:
                (launch_dir / "tui-spawn-error").write_text(
                    str(rig["tui_spawn_error"]), encoding="utf-8"
                )
                self.returncode = 2
                return
            tui_pid = Process.next_pid
            Process.next_pid += 1
            rig["tui_pid"] = tui_pid
            rig["tui_identity"] = f"{tui_pid}:stamp"
            (launch_dir / "tui-pid").write_text(str(tui_pid), encoding="utf-8")
            (launch_dir / "tui-identity").write_text(
                rig["tui_identity"], encoding="utf-8"
            )
            rig["processes"].append(_FakeTui(tui_pid))
            if rig["tui_exit_code"] is not None:
                (launch_dir / "tui-exit-code").write_text(
                    str(rig["tui_exit_code"]), encoding="utf-8"
                )

        def poll(self) -> int | None:
            if (
                rig["carrier_crash_after_registration"] is not None
                and rig["observed"].get("registered")
            ):
                return rig["carrier_crash_after_registration"]
            if rig["carrier_poll_exit"] is not None:
                return rig["carrier_poll_exit"]
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            if self.returncode is not None:
                return self.returncode
            if (
                rig["carrier_crash_after_registration"] is not None
                and rig["observed"].get("registered")
            ):
                self.returncode = rig["carrier_crash_after_registration"]
            elif rig["carrier_poll_exit"] is not None:
                self.returncode = rig["carrier_poll_exit"]
            elif rig["tui_exit_code"] is not None:
                self.returncode = 0
            else:
                self.returncode = rig["carrier_wait_code"]
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            self.terminated = True
            self.returncode = -9

    def terminate_owned_tui(launch_dir: Path, pid: int) -> None:
        rig["terminated_tuis"].append(pid)
        for item in rig["processes"]:
            if getattr(item, "pid", None) == pid and not item.is_carrier:
                item.terminated = True
                item.returncode = -15

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr(_cli_subprocess, "Popen", Process)
    monkeypatch.setattr(dsh_launch, "_terminate_owned_tui", terminate_owned_tui)
    monkeypatch.setattr(dsh_launch, "DSH_TUI_REGISTRATION_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(dsh_launch, "_DSH_CONTROL_SOCKET_PATH_MAX_BYTES", 500)
    return rig

def test_start_dsh_interactive_wires_tool_server_and_safe_injection_plugin(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    observed = launch_rig["observed"]
    assert observed["tui_argv"][0] == str(tmp_path / "bin" / "dsh-tui")
    assert observed["tui_argv"][1:5] == [
        "--backend",
        "dsh",
        "--patch",
        str(observed["patch_path"]),
    ]
    assert observed["tui_argv"][-1] == str(tmp_path)

    rows = observed["patch"][0]["insert"]
    assert [row["id"] for row in rows] == [
        "mcp-harness-bridge",
        "hyprial-dsh-bridge",
    ]
    mcp_config = rows[0]["config"]
    assert rows[0]["name"] == "@deepseek-ai/dsh-mcp-client"
    assert mcp_config["transport"] == "stdio"
    assert mcp_config["serverName"] == "harness-bridge"
    assert mcp_config["failOnStartupError"] is True
    assert mcp_config["args"][2] == "agent-channel"
    assert mcp_config["env"]["HYPRIAL_WORKER_ACTOR"] == ACTOR
    assert mcp_config["env"]["HYPRIAL_WORKER_SESSION_REF"] == SESSION_REF

    bridge_config = rows[1]["config"]
    assert rows[1]["name"] == str(observed["plugin_path"])
    assert bridge_config["socketPath"] == str(observed["control_socket"])
    assert bridge_config.get("sessionId") is None
    assert observed["control_socket"].parent == observed["launch_dir"]
    assert observed["launch_dir_mode"] == 0o700
    assert observed["plugin_mode"] == 0o600
    assert "ctx.agents.roots()" in observed["plugin_source"]
    assert "prompt.append" not in observed["plugin_source"]

    carrier_argv = observed["carrier_argv"]
    assert carrier_argv[:3] == [
        sys.executable,
        "-m",
        "hyprial.daemon.impl.mcp.entry",
    ]
    assert carrier_argv[3] == "dsh-carrier"
    assert carrier_argv[carrier_argv.index("--control-socket") + 1] == str(
        observed["control_socket"]
    )
    assert carrier_argv[carrier_argv.index("--launch-dir") + 1] == str(
        observed["launch_dir"]
    )
    assert json.loads(carrier_argv[carrier_argv.index("--tui-argv") + 1]) == (
        observed["tui_argv"]
    )
    assert observed["tui_argv"][0] == str(tmp_path / "bin" / "dsh-tui")

    env = observed["tui_env"]
    assert env["HYPRIAL_WORKER_ACTOR"] == ACTOR
    assert env["HYPRIAL_WORKER_SESSION_REF"] == SESSION_REF
    assert env["HYPRIAL_MANAGED_WORKER"] == "1"
    assert env["HYPRIAL_HOME"] == str((tmp_path / "home").resolve())
    assert observed["carrier_env"]["HYPRIAL_WORKER_ACTOR"] == ACTOR
    carrier_processes = [item for item in launch_rig["processes"] if item.is_carrier]
    assert carrier_processes
    # The carrier owns the TUI (the daemon's process fence requires the
    # registration peer to be the claimed process's direct parent), and it has
    # to stay in the terminal's session so the TUI keeps the controlling tty.
    assert "start_new_session" not in carrier_processes[0].kwargs
    tui_processes = [item for item in launch_rig["processes"] if not item.is_carrier]
    assert tui_processes and tui_processes[0].pid == launch_rig["tui_pid"]

    assert not observed["launch_dir"].exists()
    assert _leftover_launch_dirs(tmp_path) == []
    methods = [method for method, _ in launch_rig["daemon_calls"]]
    assert methods[0] == "agent.create"
    assert launch_rig["daemon_calls"][0][1]["harness"] == "dsh"
    assert launch_rig["daemon_calls"][0][1]["runtime"] == "interactive"
    assert "ps" in methods
    assert json.loads(result.stdout) == {
        "ok": True,
        "actor": ACTOR,
        "provider": "dsh",
        "runtime": "interactive",
        "sessionRef": SESSION_REF,
        "runtimeExitCode": 0,
    }

def test_start_dsh_mcp_server_argv_runs_as_dsh_mcp_client_spawns_it(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    """dsh-mcp-client spawns ``command`` with ``args`` (StdioClientTransport).

    ``args`` must therefore not repeat the interpreter: ``python python -m
    hyprial.daemon.impl.mcp.entry`` makes Python run its own binary as a script, the MCP server
    never starts, ``failOnStartupError`` drops the plugin, and the model gets
    no ``mcp__harness-bridge__*`` tool (measured in the real PTY run).
    """

    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    mcp_config = launch_rig["observed"]["patch"][0]["insert"][0]["config"]
    spawned = [mcp_config["command"], *mcp_config["args"]]
    assert spawned[:4] == [
        sys.executable,
        "-m",
        "hyprial.daemon.impl.mcp.entry",
        "agent-channel",
    ]
    probe = _REAL_POPEN(
        [*spawned[:4], "--help"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = probe.communicate(timeout=60)
    assert probe.returncode == 0, stderr[-2000:]
    assert "--actor" in stdout


def test_start_dsh_interactive_resume_pins_plugin_session_and_tui_argv(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    resume_session = "dsh-session-exact"
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent", "start",
            "dsh",
            "--name",
            "dsh-tui",
            "--cwd",
            str(tmp_path),
            "--resume",
            resume_session,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    observed = launch_rig["observed"]
    assert observed["tui_argv"][1:7] == [
        "--backend",
        "dsh",
        "--patch",
        str(observed["patch_path"]),
        "--resume",
        resume_session,
    ]
    bridge_config = observed["patch"][0]["insert"][1]["config"]
    assert bridge_config["sessionId"] == resume_session
    assert observed["tui_env"]["HYPRIAL_WORKER_SESSION_REF"] == resume_session
    assert json.loads(result.stdout)["sessionRef"] == resume_session
    assert json.loads(result.stdout)["dshSessionId"] == resume_session


def test_start_dsh_interactive_refuses_missing_binary_before_agent_create(
    launch_rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HARNESS_DSH_TUI_BIN", str(tmp_path / "missing" / "dsh-tui"))
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 1
    assert "not an executable file" in result.output
    assert all(method != "agent.create" for method, _ in launch_rig["daemon_calls"])


def test_start_dsh_interactive_refuses_model_selection_owned_by_tui(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent", "start",
            "dsh",
            "--name",
            "dsh-tui",
            "--cwd",
            str(tmp_path),
            "--provider",
            "deepseek",
            "--model",
            "deepseek-flash",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert "does not own --provider/--model" in result.output
    assert all(method != "agent.create" for method, _ in launch_rig["daemon_calls"])

def test_start_dsh_tui_spawn_failure_cleans_launch_dir(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    launch_rig["tui_spawn_error"] = OSError("spawn denied")
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 1
    assert "dsh-tui failed to start: spawn denied" in result.output
    assert _leftover_launch_dirs(tmp_path) == []


def test_start_dsh_carrier_spawn_failure_cleans_launch_dir(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    launch_rig["carrier_spawn_error"] = OSError("carrier denied")
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 1
    assert "failed to start the dsh carrier" in result.output
    assert _leftover_launch_dirs(tmp_path) == []


def test_start_dsh_registration_timeout_terminates_carrier_and_tui(
    launch_rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launch_rig["register"] = False
    monkeypatch.setattr(dsh_launch, "DSH_TUI_REGISTRATION_TIMEOUT_SECONDS", 0.05)
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 1
    assert "did not register" in result.output
    assert launch_rig["observed"].get("registered") is None
    assert launch_rig["tui_pid"] in launch_rig["terminated_tuis"]
    assert _leftover_launch_dirs(tmp_path) == []


def test_start_dsh_tui_exit_before_registration_cleans_launch_dir(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    launch_rig["register"] = False
    launch_rig["tui_exit_code"] = 7
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 1
    assert "dsh-tui exited with status 7" in result.output
    assert _leftover_launch_dirs(tmp_path) == []


def test_start_dsh_carrier_exit_before_registration_cleans_launch_dir(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    launch_rig["register"] = False
    launch_rig["carrier_poll_exit"] = 9
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 1
    assert "carrier exited with status 9" in result.output
    assert _leftover_launch_dirs(tmp_path) == []


def test_start_dsh_nonzero_tui_exit_is_reported_and_cleaned(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    launch_rig["tui_exit_code"] = 7
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["runtimeExitCode"] == 7
    assert _leftover_launch_dirs(tmp_path) == []


def test_start_dsh_carrier_crash_after_ready_stops_tui_and_reports(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    launch_rig["carrier_crash_after_registration"] = 11
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 1
    assert "dsh carrier exited with status 11" in result.output
    assert launch_rig["tui_pid"] in launch_rig["terminated_tuis"]
    tui_processes = [item for item in launch_rig["processes"] if not item.is_carrier]
    assert tui_processes[0].terminated is True
    assert _leftover_launch_dirs(tmp_path) == []


def test_start_dsh_carrier_crash_quotes_the_carrier_stderr_tail(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    """The carrier's stderr lives in the launch dir, which teardown removes;
    the failure message must carry its tail instead."""

    launch_rig["carrier_crash_after_registration"] = 11
    launch_rig["carrier_stderr"] = "".join(
        f"noise line {index}\n" for index in range(40)
    ) + "Traceback: CarrierBoom\n"
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 1
    assert "dsh carrier exited with status 11" in result.output
    assert "Traceback: CarrierBoom" in result.output
    assert "noise line 0" not in result.output
    assert _leftover_launch_dirs(tmp_path) == []


def test_start_dsh_does_not_claim_ready_from_a_different_carrier_source(
    launch_rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launch_rig["session_source"] = "pi-extension"
    monkeypatch.setattr(dsh_launch, "DSH_TUI_REGISTRATION_TIMEOUT_SECONDS", 0.05)
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 1
    assert "did not register" in result.output
    assert _leftover_launch_dirs(tmp_path) == []


def test_start_dsh_boots_the_standard_agent_preset_by_default(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert launch_rig["observed"]["tui_env"]["DSH_TUI_PRESET"] == "standard"


def test_start_dsh_honours_an_agent_preset_override(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent", "start",
            "dsh",
            "--name",
            "dsh-tui",
            "--cwd",
            str(tmp_path),
            "--agent-preset",
            "liangshen",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert launch_rig["observed"]["tui_env"]["DSH_TUI_PRESET"] == "liangshen"


def test_start_dsh_rejects_an_invalid_agent_preset_id(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent", "start",
            "dsh",
            "--name",
            "dsh-tui",
            "--cwd",
            str(tmp_path),
            "--agent-preset",
            "Not_Valid",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert "--agent-preset" in result.output
    assert all(method != "agent.create" for method, _ in launch_rig["daemon_calls"])


def test_start_dsh_agent_preset_is_refused_off_interactive_dsh(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent", "start",
            "dsh",
            "--name",
            "dsh-tui",
            "--cwd",
            str(tmp_path),
            "--headless",
            "--agent-preset",
            "standard",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert "--agent-preset" in result.output
    assert all(method != "agent.create" for method, _ in launch_rig["daemon_calls"])


def test_start_dsh_marks_a_fresh_home_first_run_seen(
    launch_rig: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))

    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    data_dir = user_home / ".dsh-tui"
    assert (data_dir / "onboarding.json").exists()
    onboarding = json.loads((data_dir / "onboarding.json").read_text())
    assert onboarding == {"completed": True, "version": 1}
    assert (data_dir / "home.json").exists()
    assert json.loads((data_dir / "home.json").read_text()) == {"seen": True}


def test_start_dsh_never_overwrites_existing_first_run_prefs(
    launch_rig: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user-home"
    data_dir = user_home / ".dsh-tui"
    data_dir.mkdir(parents=True)
    onboarding = data_dir / "onboarding.json"
    home = data_dir / "home.json"
    onboarding.write_text('{"completed": false, "version": 0}\n')
    home.write_text('{"seen": false}\n')
    monkeypatch.setenv("HOME", str(user_home))

    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert onboarding.read_text() == '{"completed": false, "version": 0}\n'
    assert home.read_text() == '{"seen": false}\n'


def test_start_dsh_rejects_unsafe_resume_id_before_agent_create(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent", "start",
            "dsh",
            "--name",
            "dsh-tui",
            "--cwd",
            str(tmp_path),
            "--resume",
            "../../escape",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert "must be a DSH session id" in result.output
    assert all(method != "agent.create" for method, _ in launch_rig["daemon_calls"])


def test_start_dsh_rejects_positional_runtime_args(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent", "start",
            "dsh",
            "--name",
            "dsh-tui",
            "--cwd",
            str(tmp_path),
            "--json",
            "--",
            "hello",
        ],
    )

    assert result.exit_code == 1
    assert "positional dsh-tui runtime argument" in result.output
    assert all(method != "agent.create" for method, _ in launch_rig["daemon_calls"])


def test_start_dsh_rejects_owned_backend_flag_after_separator(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    result = CliRunner().invoke(
        _cli_root_app,
        [
            "agent", "start",
            "dsh",
            "--name",
            "dsh-tui",
            "--cwd",
            str(tmp_path),
            "--json",
            "--",
            "--backend",
            "claude",
        ],
    )

    assert result.exit_code == 1
    assert "changes the dsh-tui launch contract" in result.output
    assert all(method != "agent.create" for method, _ in launch_rig["daemon_calls"])


def test_start_dsh_rejects_too_long_control_socket_path(
    launch_rig: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(dsh_launch, "_DSH_CONTROL_SOCKET_PATH_MAX_BYTES", 10)
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 1
    assert "control socket path is too long" in result.output


def test_start_dsh_carrier_crash_names_the_dsh_session_to_resume(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    launch_rig["carrier_crash_after_registration"] = 11
    launch_rig["dsh_session_id"] = "dsh-session-7"
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 1
    assert "dsh-session-7" in result.output
    assert "hyprial agent start dsh --resume dsh-session-7" in result.output


def test_start_dsh_launcher_unregisters_only_its_fenced_session(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    launch_rig["carrier_crash_after_registration"] = 11
    launch_rig["session_process_pid"] = "tui"
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 1
    methods = [method for method, _ in launch_rig["daemon_calls"]]
    assert "session.unregister" in methods


def test_start_dsh_launcher_keeps_a_newer_registration_with_another_pid(
    launch_rig: dict[str, Any], tmp_path: Path
) -> None:
    launch_rig["carrier_crash_after_registration"] = 11
    launch_rig["session_process_pid"] = 999
    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "start", "dsh", "--name", "dsh-tui", "--cwd", str(tmp_path), "--json"],
    )

    assert result.exit_code == 1
    methods = [method for method, _ in launch_rig["daemon_calls"]]
    assert "session.unregister" not in methods
