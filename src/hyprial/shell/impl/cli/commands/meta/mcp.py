"""``hyprial mcp`` local MCP adapter channels."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.kernel import Logger
from pathlib import Path
from hyprial.kernel import ipc_errors
import os
from typing import Any
import signal
import typer

from hyprial.shell.impl.cli.commands.common.support import _resolved_agent_cwd
mcp_app = typer.Typer(help="Run local MCP adapters.")

#: The carrier's own stderr, inside the launch directory (see _spawn_owned_tui).
DSH_CARRIER_STDERR_FILENAME = "carrier.stderr"


@mcp_app.command("claude-channel")
def mcp_claude_channel(
    actor: str = typer.Option(..., "--actor"),
    session_ref: str = typer.Option(..., "--session-ref"),
    cwd: Path | None = typer.Option(None, "--cwd"),
    command: list[str] | None = typer.Option(None, "--command"),
    poll_interval: float = typer.Option(0.5, "--poll-interval", hidden=True),
    recovery_signal: Path | None = typer.Option(None, "--recovery-signal", hidden=True),
    turn_signal_dir: Path | None = typer.Option(
        None,
        "--turn-signal-dir",
        hidden=True,
        help="Per-launch directory of Stop-hook pulses to forward as turn reports.",
    ),
    owner_pid: int | None = typer.Option(None, "--owner-pid", hidden=True),
    owner_identity: str | None = typer.Option(None, "--owner-identity", hidden=True),
    tmux_session: str | None = typer.Option(None, "--tmux-session", hidden=True),
) -> None:
    """Run one Claude Code-owned stdio Channel server."""
    services = get_services()

    services.require_initialized_hyprial_home()

    import anyio

    from hyprial.daemon import (
        StatelessDaemonProxy,
        UnixDaemonConnectionFactory,
        serve_channel_stdio,
    )

    async def run() -> None:
        services = get_services()
        proxy = StatelessDaemonProxy(UnixDaemonConnectionFactory(services._socket_path()))
        await serve_channel_stdio(
            proxy,
            actor=actor,
            session_ref=session_ref,
            cwd=str(_resolved_agent_cwd(actor, cwd)),
            command=tuple(command or ("claude",)),
            logger=Logger.worker(services._state_dir(), runtime="claude", name=actor),
            poll_interval=poll_interval,
            recovery_signal=recovery_signal,
            turn_signal_dir=turn_signal_dir,
            owner_pid=owner_pid,
            owner_identity=owner_identity,
            tmux_session=tmux_session,
        )

    # stdout belongs exclusively to the MCP stdio wire.
    anyio.run(run)


def _install_carrier_signal_handlers(stop_event: object) -> dict[int, object]:
    """SIGTERM/SIGHUP stop the carrier; SIGINT belongs to the TUI child.

    A closed terminal must unregister, while Ctrl-C in the interactive child
    must not tear the bridge down before it can clean up.
    """

    previous = {
        signal.SIGTERM: signal.signal(
            signal.SIGTERM, lambda *_args: stop_event.set()  # type: ignore[attr-defined]
        ),
        signal.SIGHUP: signal.signal(
            signal.SIGHUP, lambda *_args: stop_event.set()  # type: ignore[attr-defined]
        ),
        signal.SIGINT: signal.signal(signal.SIGINT, signal.SIG_IGN),
    }
    return previous


def _spawn_owned_tui(tui_argv: list[str], *, cwd: str, launch_dir: Path) -> Any:
    """Start the TUI on the terminal, then move the carrier's stderr off it.

    The TUI inherits the terminal's stdio and owns the screen; anything the
    carrier writes to fd 2 afterwards (warnings, log records, tracebacks)
    would draw over it. The carrier's fd 2 therefore becomes an owner-only
    file in the launch directory, which the launcher quotes on failure.
    """

    import subprocess
    import sys

    process = subprocess.Popen(tui_argv, cwd=cwd)
    path = Path(launch_dir) / DSH_CARRIER_STDERR_FILENAME
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        sys.stderr.flush()
        os.dup2(descriptor, 2)
    finally:
        os.close(descriptor)
    return process


def _restore_carrier_signal_handlers(previous: dict[int, object]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)  # type: ignore[arg-type]


@mcp_app.command("dsh-carrier", hidden=True)
def mcp_dsh_carrier(
    actor: str = typer.Option(..., "--actor", help="Actor URI this launch registers."),
    session_ref: str = typer.Option(
        ..., "--session-ref", help="Hyprial interactive session ref for the launch."
    ),
    cwd: Path = typer.Option(
        ..., "--cwd", help="Working directory of the dsh-tui process."
    ),
    control_socket: Path = typer.Option(
        ..., "--control-socket", help="Launch-scoped plugin control socket."
    ),
    launch_dir: Path = typer.Option(
        ..., "--launch-dir", help="Launch directory this carrier publishes state to."
    ),
    tui_argv_json: str = typer.Option(
        ..., "--tui-argv", help="JSON argv of the dsh-tui process this carrier owns."
    ),
    poll_interval: float = typer.Option(
        0.5, "--poll-interval", help="Daemon poll interval in seconds."
    ),
) -> None:
    """Own one dsh-tui launch: spawn the TUI, register, wake, unregister.

    The carrier is the registration peer, so the daemon's process fence requires
    the claimed process to be its direct child. Starting the TUI here (instead
    of in the launcher) is what satisfies that relation; the launcher learns the
    pid, identity and exit code from the launch directory.
    """
    services = get_services()

    services.require_initialized_hyprial_home()

    import anyio
    import json
    import subprocess
    import threading
    import time

    from hyprial.daemon import (
        DSH_TUI_EXIT_CODE_FILENAME,
        DSH_TUI_IDENTITY_FILENAME,
        DSH_TUI_PID_FILENAME,
        DSH_TUI_SPAWN_ERROR_FILENAME,
        StatelessDaemonProxy,
        UnixDaemonConnectionFactory,
        process_identity_for_tui,
        run_dsh_carrier,
        write_launch_state,
    )

    try:
        tui_argv = json.loads(tui_argv_json)
    except ValueError as error:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT, f"--tui-argv is not valid JSON: {error}"
        ) from error
    if (
        not isinstance(tui_argv, list)
        or not tui_argv
        or not all(isinstance(item, str) for item in tui_argv)
    ):
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "--tui-argv must be a non-empty JSON array of strings",
        )
    resolved_cwd = str(_resolved_agent_cwd(actor, cwd))
    try:
        tui_process = _spawn_owned_tui(
            tui_argv, cwd=resolved_cwd, launch_dir=launch_dir
        )
    except OSError as error:
        write_launch_state(launch_dir, DSH_TUI_SPAWN_ERROR_FILENAME, str(error))
        raise services.CliError(
            "DSH_TUI_SPAWN_FAILED", f"failed to start dsh-tui: {error}"
        ) from error
    identity = None
    for _attempt in range(20):
        identity = process_identity_for_tui(tui_process.pid)
        if identity:
            break
        time.sleep(0.05)
    if not identity:
        tui_process.terminate()
        try:
            tui_process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            tui_process.kill()
            tui_process.wait()
        message = (
            "cannot read a birth identity for the dsh-tui process"
            f" (pid {tui_process.pid})"
        )
        write_launch_state(launch_dir, DSH_TUI_SPAWN_ERROR_FILENAME, message)
        raise services.CliError(ipc_errors.INVALID_ARGUMENT, message)
    write_launch_state(launch_dir, DSH_TUI_PID_FILENAME, str(tui_process.pid))
    write_launch_state(launch_dir, DSH_TUI_IDENTITY_FILENAME, identity)

    stop_event = threading.Event()
    tui_exit: dict[str, int | None] = {"code": None}

    def watch_tui() -> None:
        tui_exit["code"] = tui_process.wait()
        stop_event.set()

    tui_watcher = threading.Thread(
        target=watch_tui, name=f"hyprial-dsh-tui-watch-{actor}", daemon=True
    )
    tui_watcher.start()

    async def run() -> None:
        services = get_services()
        proxy = StatelessDaemonProxy(
            UnixDaemonConnectionFactory(services._socket_path())
        )
        await run_dsh_carrier(
            proxy,
            actor=actor,
            session_ref=session_ref,
            cwd=resolved_cwd,
            command=("dsh-tui",),
            control_socket=control_socket,
            tui_pid=tui_process.pid,
            tui_identity=identity,
            logger=Logger.worker(services._state_dir(), runtime="dsh", name=actor),
            poll_interval=poll_interval,
            stop_event=stop_event,
        )

    previous_handlers = _install_carrier_signal_handlers(stop_event)
    try:
        anyio.run(run)
    finally:
        _restore_carrier_signal_handlers(previous_handlers)
        if tui_process.poll() is None:
            tui_process.terminate()
        try:
            tui_process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            tui_process.kill()
            tui_process.wait()
        tui_watcher.join(timeout=2.0)
        write_launch_state(
            launch_dir,
            DSH_TUI_EXIT_CODE_FILENAME,
            str(tui_process.returncode),
        )


@mcp_app.command("claude-channel-recover", hidden=True)
def mcp_claude_channel_recover(
    signal_path: Path = typer.Option(..., "--signal-path"),
) -> None:
    """Pulse one running Claude Channel after a lifecycle/model failure."""
    services = get_services()

    services.require_initialized_hyprial_home()

    from hyprial.daemon import signal_channel_recovery

    signal_channel_recovery(signal_path.expanduser().resolve())


@mcp_app.command("claude-turn-ended", hidden=True)
def mcp_claude_turn_ended(
    signal_dir: Path = typer.Option(
        ..., "--signal-dir", help="Per-launch pulse directory the channel child forwards."
    ),
) -> None:
    """Record one silent, best-effort Claude Stop pulse for the channel child."""

    try:
        from hyprial.biz import signal_turn_ended

        signal_turn_ended(signal_dir.expanduser().resolve())
    except Exception:
        # A Stop hook is telemetry, never a gate. Empty stdout plus a normal
        # return keeps it out of Claude's context and cannot prolong the turn.
        return


class SessionRefFileProxy:
    """Sign calls with the sessionRef published after launch (placeholder until then)."""

    def __init__(self, inner: Any, path: Path, placeholder: str) -> None:
        self._inner, self._path, self._placeholder = inner, path, placeholder

    async def call(self, **kwargs: Any) -> Any:
        try:
            kwargs["session_ref"] = self._path.read_text().strip() or self._placeholder
        except OSError:
            kwargs["session_ref"] = self._placeholder
        return await self._inner.call(**kwargs)


@mcp_app.command("agent-channel")
def mcp_agent_channel(
    actor: str = typer.Option(..., "--actor"),
    session_ref: str | None = typer.Option(None, "--session-ref", hidden=True),
    session_ref_file: Path | None = typer.Option(
        None, "--session-ref-file", hidden=True, help="Path to a file holding the session ref (managed launch only)."
    ),
) -> None:
    """Run one managed-worker stdio MCP server with a fixed daemon identity."""
    services = get_services()

    services.require_initialized_hyprial_home()
    session_ref = session_ref or os.environ.get("HYPRIAL_WORKER_SESSION_REF")
    if session_ref_file is not None:
        session_ref = session_ref or "pending"
    if not session_ref:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "managed worker channel requires HYPRIAL_WORKER_SESSION_REF",
        )

    import anyio

    from hyprial.daemon import (
        StatelessDaemonProxy,
        UnixDaemonConnectionFactory,
        serve_worker_stdio,
    )

    async def run() -> None:
        services = get_services()
        proxy = StatelessDaemonProxy(UnixDaemonConnectionFactory(services._socket_path()))
        if session_ref_file is not None:
            proxy = SessionRefFileProxy(proxy, session_ref_file, session_ref)  # type: ignore[assignment]
        await serve_worker_stdio(proxy, actor=actor, session_ref=session_ref)

    # stdout belongs exclusively to the MCP stdio wire.
    anyio.run(run)
