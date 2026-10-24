"""The `hyprial agent start dsh` launch contract (dsh-tui, carrier, bridge)."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import warn

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from pathlib import Path
from hyprial.kernel import ipc_errors
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

import yaml

from hyprial.shell.impl.cli.commands.common.support import JsonObject

from hyprial.shell.impl.cli.commands.agent.runtime.interactive import (
    _handover_prompt,
    _interactive_actor,
    _interactive_actor_name,
    _terminate_process,
    _wait_foreground,
)


DSH_TUI_REGISTRATION_TIMEOUT_SECONDS = 30.0
# Allen's workspace-start ruling (2026-10-19): `hyprial agent start dsh` boots the
# dsh-tui agent preset `standard` unless the launch overrides it. The value is
# handed to the TUI as the documented DSH_TUI_PRESET launch instruction, which
# outranks the persisted /preset choice and the cordis.yml default.
DSH_TUI_DEFAULT_AGENT_PRESET = "standard"
_DSH_TUI_PRESET_VARIABLE = "DSH_TUI_PRESET"
# dsh-agent-presets' own id boundary (dsh-tui presetPrefs.js PRESET_ID); a
# value outside it would compose a session without a preset, so refuse it here.
_DSH_AGENT_PRESET_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_DSH_TUI_DATA_DIRNAME = ".dsh-tui"
_DSH_TUI_ONBOARDING_FILENAME = "onboarding.json"
_DSH_TUI_HOME_FILENAME = "home.json"
# dsh-tui 0.16.x ONBOARDING_VERSION: the wizard revision this launch marks done.
_DSH_TUI_ONBOARDING_VERSION = 1
_DSH_OWNED_RUNTIME_FLAGS = frozenset(
    {
        "--",
        "--backend",
        "--patch",
        "--profile",
        "--from-default-profile",
        "--resume",
        "-c",
        "--continue",
    }
)
_DSH_TUI_SUBCOMMANDS = frozenset(
    {"doctor", "help", "migrate", "safe", "update", "version"}
)
_DSH_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DSH_CONTROL_SOCKET_PATH_MAX_BYTES = 100

def _dsh_tui_binary() -> str:
    """Resolve the TUI binary before any daemon mutation."""

    services = get_services()
    override = os.environ.get("HARNESS_DSH_TUI_BIN")
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"HARNESS_DSH_TUI_BIN is not an executable file: {override}",
            )
        return str(candidate.resolve())
    binary = shutil.which("dsh-tui")
    if binary is None:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "dsh-tui binary not found on PATH; install it or set "
            "HARNESS_DSH_TUI_BIN",
        )
    return binary


def _seed_dsh_tui_first_run_prefs(home: str | Path | None) -> None:
    """Mark dsh-tui's first-run guide and one-shot workspace home seen.

    ``hyprial agent start dsh`` owns the dsh-tui launch contract, and the
    workspace-start ruling (2026-10-19) is that the launch lands straight in a
    workspace with an agent running. On a fresh HOME dsh-tui instead opens its
    four-step first-run wizard and then the one-shot workspace home before any
    agent the user asked for, so the launcher writes the "seen" state the TUI
    gates those screens on before the carrier spawns it. Only a missing file
    is created (O_EXCL): an existing pref is the user's own state and is never
    overwritten. Best effort: a write failure only means dsh-tui may show its
    wizard, exactly as it would without hyprial; the wizard stays available
    inside the TUI through /setup.
    """

    if home is None:
        return
    data_dir = Path(home) / _DSH_TUI_DATA_DIRNAME
    payloads = (
        (
            _DSH_TUI_ONBOARDING_FILENAME,
            {"completed": True, "version": _DSH_TUI_ONBOARDING_VERSION},
        ),
        (_DSH_TUI_HOME_FILENAME, {"seen": True}),
    )
    try:
        data_dir.mkdir(mode=0o700, exist_ok=True)
        for name, payload in payloads:
            path = data_dir / name
            if path.exists():
                continue
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(payload) + "\n")
    except OSError as error:
        warn(
            "hyprial: warning: could not mark the dsh-tui first-run screens "
            f"seen in {data_dir}: {error}",
            json_output=False,
        )


def _reject_dsh_owned_runtime_args(runtime_args: tuple[str, ...]) -> None:
    services = get_services()
    if runtime_args and runtime_args[0] in _DSH_TUI_SUBCOMMANDS:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"dsh-tui subcommand {runtime_args[0]} is not supported through hyprial agent start dsh",
        )
    for arg in runtime_args:
        if not arg.startswith("-"):
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"positional dsh-tui runtime argument {arg} is not supported; "
                "interactive dsh accepts no prompt positional through hyprial agent start",
            )
        flag = arg.split("=", 1)[0]
        if flag in _DSH_OWNED_RUNTIME_FLAGS:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                f"{flag} changes the dsh-tui launch contract owned by hyprial; "
                "use `hyprial agent start dsh --resume <session>` for resume",
            )


def _write_dsh_launch_patch(
    path: Path,
    *,
    actor_name: str,
    session_ref: str,
    plugin_path: Path,
    control_socket: Path,
    mcp_env: dict[str, str],
    dsh_session_id: str | None,
) -> None:
    """Write one launch-scoped dsh profile overlay, refusing overwrites."""

    args = [
        "-m",
        "hyprial.daemon.impl.mcp.entry",
        "agent-channel",
        "--actor",
        actor_name,
        "--session-ref",
        session_ref,
    ]
    bridge_config: dict[str, object] = {"socketPath": str(control_socket)}
    if dsh_session_id is not None:
        bridge_config["sessionId"] = dsh_session_id
    from hyprial.daemon import dsh_mcp_client_row

    patch = [
        {
            "insert": [
                dsh_mcp_client_row(
                    row_id="mcp-harness-bridge",
                    server_name="harness-bridge",
                    command=sys.executable,
                    args=args,
                    env=mcp_env,
                ),
                {
                    "id": "hyprial-dsh-bridge",
                    "name": str(plugin_path),
                    "config": bridge_config,
                },
            ]
        }
    ]
    text = yaml.safe_dump(
        patch,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


def _dsh_session_id_from_launch(launch_dir: Path, resume: str | None) -> str | None:
    """Name the DSH session a failed bridge was attached to, if known."""

    if resume:
        return resume
    try:
        value = (launch_dir / "dsh-session-id").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


_DSH_CARRIER_STDERR_FILENAME = "carrier.stderr"
_DSH_CARRIER_STDERR_TAIL_LINES = 20


def _dsh_carrier_stderr_tail(launch_dir: Path) -> str | None:
    """The last lines the carrier wrote to its (off-terminal) stderr."""

    try:
        text = (launch_dir / _DSH_CARRIER_STDERR_FILENAME).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    return "\n".join(lines[-_DSH_CARRIER_STDERR_TAIL_LINES:])


def _dsh_carrier_failure(
    detail: str, launch_dir: Path, resume: str | None
) -> str:
    session_id = _dsh_session_id_from_launch(launch_dir, resume)
    if session_id is not None:
        detail = (
            f"{detail}; the DSH session was {session_id} - resume it with "
            f"`hyprial agent start dsh --resume {session_id}`"
        )
    tail = _dsh_carrier_stderr_tail(launch_dir)
    if tail is not None:
        detail = f"{detail}\n\ncarrier stderr (last lines):\n{tail}"
    return detail


def _fenced_dsh_unregister(
    services: Any, actor: str, session_ref: str, tui_pid: int
) -> None:
    """Withdraw only the registration this launch still owns.

    The carrier normally unregisters itself. When it dies first (SIGKILL, or a
    closed terminal) the launcher is the last survivor and must clean up, but
    a newer same-actor registration may already exist. The stored process
    fence has to name this launch's TUI process before the unregister is sent.
    """

    try:
        status = services._daemon_request("ps")
    except Exception:  # noqa: BLE001 - teardown must not mask the launch result
        return
    sessions = status.get("interactiveSessions", [])
    if not isinstance(sessions, list):
        return
    owned = any(
        isinstance(item, dict)
        and item.get("actor") == actor
        and item.get("sessionRef") == session_ref
        and item.get("source") == "dsh-channel"
        and item.get("processPid") == tui_pid
        for item in sessions
    )
    if not owned:
        return
    try:
        services._daemon_request(
            "session.unregister",
            {"actor": actor, "sessionRef": session_ref},
            restore_wait=0.0,
        )
    except Exception:  # noqa: BLE001 - best effort, never masks the exit
        return


def _read_launch_state(launch_dir: Path, name: str) -> str | None:
    try:
        value = (launch_dir / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _read_launch_int(launch_dir: Path, name: str) -> int | None:
    value = _read_launch_state(launch_dir, name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _terminate_owned_tui(launch_dir: Path, pid: int) -> None:
    """Terminate the TUI the carrier owned, fenced by its birth identity.

    The carrier is the TUI's parent, so a launcher only reaches this path when
    the carrier died first and left the TUI orphaned. Comparing the published
    identity before signalling keeps a recycled pid out of reach.
    """

    from hyprial.daemon import DSH_TUI_IDENTITY_FILENAME, process_identity_for_tui

    expected = _read_launch_state(launch_dir, DSH_TUI_IDENTITY_FILENAME)
    observed = process_identity_for_tui(pid)
    if observed is None or (expected is not None and observed != expected):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _attempt in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _dsh_session_registered(
    actor: str, session_ref: str, status: JsonObject
) -> bool:
    sessions = status.get("interactiveSessions", [])
    return isinstance(sessions, list) and any(
        isinstance(item, dict)
        and item.get("actor") == actor
        and item.get("sessionRef") == session_ref
        and item.get("source") == "dsh-channel"
        for item in sessions
    )


def _start_interactive_dsh(
    *,
    name: str,
    nickname: str | None,
    cwd: Path,
    resume: str | None,
    runtime_args: tuple[str, ...],
    model_provider: str | None,
    model: str | None,
    json_output: bool,
    tmux: bool = False,
    agent_preset: str | None = None,
) -> JsonObject:
    """Launch dsh-tui with a launch-scoped MCP tool server and carrier."""

    services = get_services()
    if tmux:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "--tmux is not supported for interactive dsh",
        )
    if agent_preset is None:
        agent_preset = DSH_TUI_DEFAULT_AGENT_PRESET
    elif _DSH_AGENT_PRESET_RE.fullmatch(agent_preset) is None:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"invalid --agent-preset {agent_preset!r}: a dsh agent preset id is "
            "lowercase letters, digits and dashes, starting with a letter or digit",
        )
    binary = _dsh_tui_binary()
    _reject_dsh_owned_runtime_args(runtime_args)
    if model_provider is not None or model is not None:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "interactive dsh does not own --provider/--model yet; choose the "
            "model inside dsh-tui",
        )
    if resume is not None:
        resume = resume.strip()
        if not resume:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT, "--resume requires a session id"
            )
        if _DSH_SESSION_ID_RE.fullmatch(resume) is None:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--resume must be a DSH session id containing only letters, "
                "digits, dot, underscore or dash",
            )
    handover = _handover_prompt(
        services._create_agent_for_start(
            name=name,
            harness="dsh",
            runtime="interactive",
            cwd=cwd,
            provider=model_provider,
            model=model,
        )
    )
    status = services._daemon_request("ps")
    actor = _interactive_actor(name, status)
    actor_name = _interactive_actor_name(name)
    display_name = nickname or actor_name
    session_ref = resume if resume is not None else str(services.uuid4())
    state_dir = services._state_dir()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    from hyprial.daemon import (
        DSH_TUI_EXIT_CODE_FILENAME,
        DSH_TUI_PID_FILENAME,
        DSH_TUI_SPAWN_ERROR_FILENAME,
        write_dsh_tui_plugin,
    )
    from hyprial.kernel import child_state_environment

    launch_dir = state_dir / f"dsh-tui-{services.uuid4().hex[:16]}"
    launch_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(launch_dir, 0o700)
    patch_path = launch_dir / "profile.patch.yml"
    plugin_path = launch_dir / "hyprial-bridge-plugin.mjs"
    control_socket = launch_dir / "bridge.sock"
    if len(os.fsencode(str(control_socket))) > _DSH_CONTROL_SOCKET_PATH_MAX_BYTES:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "the isolated DSH control socket path is too long for a Unix "
            f"socket ({len(os.fsencode(str(control_socket)))} bytes); set a "
            "shorter HARNESS_STATE_DIR",
        )
    hyprial_home = services._hyprial_home()
    mcp_env = child_state_environment(hyprial_home, state_dir)
    mcp_env.update(
        {
            "HYPRIAL_WORKER_ACTOR": actor,
            "HYPRIAL_WORKER_SESSION_REF": session_ref,
            "HYPRIAL_MANAGED_WORKER": "1",
        }
    )
    carrier_process: subprocess.Popen[Any] | None = None
    watcher: threading.Thread | None = None
    watch_stop = threading.Event()
    carrier_failure: dict[str, str | None] = {"error": None}
    returncode = 1
    try:
        write_dsh_tui_plugin(launch_dir)
        _write_dsh_launch_patch(
            patch_path,
            actor_name=actor_name,
            session_ref=session_ref,
            plugin_path=plugin_path,
            control_socket=control_socket,
            mcp_env=mcp_env,
            dsh_session_id=resume,
        )
        environment = dict(os.environ)
        environment.update(mcp_env)
        environment[_DSH_TUI_PRESET_VARIABLE] = agent_preset
        # The TUI reads these prefs once at boot; they must be in place before
        # the carrier spawns it (see _seed_dsh_tui_first_run_prefs).
        _seed_dsh_tui_first_run_prefs(environment.get("HOME"))
        tui_argv = [binary, "--backend", "dsh", "--patch", str(patch_path)]
        if resume is not None:
            tui_argv.extend(["--resume", resume])
        tui_argv.extend(runtime_args)
        tui_argv.append(str(cwd))
        # The carrier is the registration peer, and the daemon's process fence
        # accepts a claimed process that is the peer's direct child. The carrier
        # therefore spawns the TUI; the launcher only hands it the argv and reads
        # the pid/identity/exit-code files it publishes.
        carrier_argv = [
            sys.executable,
            "-m",
            "hyprial.daemon.impl.mcp.entry",
            "dsh-carrier",
            "--actor",
            actor_name,
            "--session-ref",
            session_ref,
            "--cwd",
            str(cwd),
            "--control-socket",
            str(control_socket),
            "--launch-dir",
            str(launch_dir),
            "--tui-argv",
            json.dumps(tui_argv),
        ]
        try:
            carrier_process = subprocess.Popen(
                carrier_argv,
                cwd=cwd,
                env=environment,
                stdout=sys.stderr if json_output else None,
                stderr=sys.stderr,
            )
        except OSError as error:
            raise services.CliError(
                "DSH_CARRIER_SPAWN_FAILED",
                f"failed to start the dsh carrier: {error}",
            ) from error
        registered = False
        deadline = services.time.monotonic() + DSH_TUI_REGISTRATION_TIMEOUT_SECONDS
        while services.time.monotonic() < deadline:
            status = services._daemon_request("ps")
            registered = _dsh_session_registered(actor, session_ref, status)
            if registered or carrier_process.poll() is not None:
                break
            services.time.sleep(0.1)
        if not registered:
            carrier_exit = carrier_process.poll()
            spawn_error = _read_launch_state(
                launch_dir, DSH_TUI_SPAWN_ERROR_FILENAME
            )
            tui_exit = _read_launch_int(launch_dir, DSH_TUI_EXIT_CODE_FILENAME)
            detail = (
                f"dsh-tui failed to start: {spawn_error}"
                if spawn_error is not None
                else f"dsh-tui exited with status {tui_exit}"
                if tui_exit is not None
                else f"carrier exited with status {carrier_exit}"
                if carrier_exit is not None
                else "registration timed out"
            )
            raise services.CliError(
                "CHANNEL_REGISTRATION_FAILED",
                _dsh_carrier_failure(
                    f"interactive dsh did not register {actor}: {detail}; the "
                    "launch-scoped MCP tools and safe injection plugin must "
                    "both be ready before a carrier may claim online",
                    launch_dir,
                    resume,
                )
                + (f"\n\nHarness handover: {handover}" if handover else ""),
            )

        def watch_children() -> None:
            while not watch_stop.is_set():
                carrier_code = carrier_process.poll()
                if carrier_code is not None:
                    tui_exit = _read_launch_int(
                        launch_dir, DSH_TUI_EXIT_CODE_FILENAME
                    )
                    if tui_exit is None:
                        carrier_failure["error"] = (
                            f"dsh carrier exited with status {carrier_code}"
                        )
                        tui_pid = _read_launch_int(
                            launch_dir, DSH_TUI_PID_FILENAME
                        )
                        if tui_pid is not None:
                            _terminate_owned_tui(launch_dir, tui_pid)
                    return
                time.sleep(0.2)

        watcher = threading.Thread(
            target=watch_children,
            name=f"hyprial-dsh-carrier-watch-{actor_name}",
            daemon=True,
        )
        watcher.start()
        carrier_code = _wait_foreground(carrier_process)
        tui_code = _read_launch_int(launch_dir, DSH_TUI_EXIT_CODE_FILENAME)
        failure = carrier_failure["error"]
        if tui_code is None and carrier_code != 0:
            # The carrier died without publishing a TUI exit code: report the
            # carrier, and take the orphaned TUI down from the published pid.
            failure = failure or f"dsh carrier exited with status {carrier_code}"
            tui_pid = _read_launch_int(launch_dir, DSH_TUI_PID_FILENAME)
            if tui_pid is not None:
                _terminate_owned_tui(launch_dir, tui_pid)
        if failure is not None:
            raise services.CliError(
                "DSH_CARRIER_EXITED",
                _dsh_carrier_failure(failure, launch_dir, resume),
            )
        returncode = tui_code if tui_code is not None else carrier_code
    finally:
        watch_stop.set()
        if carrier_process is not None and carrier_process.poll() is None:
            _terminate_process(carrier_process)
        tui_pid = _read_launch_int(launch_dir, DSH_TUI_PID_FILENAME)
        if tui_pid is not None:
            _terminate_owned_tui(launch_dir, tui_pid)
        if watcher is not None:
            watcher.join(timeout=2.0)
        if carrier_process is not None and tui_pid is not None:
            _fenced_dsh_unregister(services, actor, session_ref, tui_pid)
        shutil.rmtree(launch_dir, ignore_errors=True)
    return {
        "ok": returncode == 0,
        "actor": actor,
        "provider": "dsh",
        "runtime": "interactive",
        "sessionRef": session_ref,
        "runtimeExitCode": returncode,
        **({"dshSessionId": resume} if resume is not None else {}),
        **({"displayName": display_name} if nickname is not None else {}),
    }
