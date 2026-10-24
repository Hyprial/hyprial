"""Module-private process entries for the local MCP channel servers.

The interactive launcher and the daemon spawn these stdio servers directly, so
R11 moved them out of the ``hyprial`` CLI tree: there is no ``mcp`` command and
no public spelling.  The argv below is still an internal contract between the
spawn site and this process, and each subcommand mirrors exactly the flags the
launcher/daemon writes::

    python -m hyprial.daemon.impl.mcp.entry claude-channel --actor A --session-ref S ...
    python -m hyprial.daemon.impl.mcp.entry claude-channel-recover --signal-path P
    python -m hyprial.daemon.impl.mcp.entry agent-channel --actor A
    python -m hyprial.daemon.impl.mcp.entry dsh-carrier --actor A ...

``claude-channel-recover`` stays here (not in a stdlib-only module) because it
is the SessionStart/StopFailure hook the launcher embeds in ``--settings`` for
an already-running session; it must keep the same startup shape as the server
it pulses.  ``mcp claude-turn-ended`` was deleted: its Stop-hook job is owned
by the stdlib ``hyprial.biz.impl.turn_pulse`` entry.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

#: The carrier's own stderr, inside the launch directory.
DSH_CARRIER_STDERR_FILENAME = "carrier.stderr"


def _state_dir() -> Path:
    from hyprial.kernel import configured_harness_state_dir

    return configured_harness_state_dir()


def _socket_path() -> Path:
    from hyprial.daemon.impl.orgfs.webserver.ipc import default_daemon_socket

    return default_daemon_socket()


def _agent_workspace(actor: str) -> Path:
    from hyprial.identity import AgentRegistry
    from hyprial.kernel import configured_hyprial_home

    name = AgentRegistry.normalize_actor(actor)
    return configured_hyprial_home()[0] / "agents" / name / "workspace"


def _resolved_agent_cwd(actor: str, cwd: Path | None) -> Path:
    return cwd.expanduser().resolve() if cwd is not None else _agent_workspace(actor)


def _run_claude_channel(args: argparse.Namespace) -> int:
    from hyprial.kernel import Logger, require_initialized_hyprial_home

    require_initialized_hyprial_home()

    import anyio

    from hyprial.daemon import (
        StatelessDaemonProxy,
        UnixDaemonConnectionFactory,
        serve_channel_stdio,
    )

    state_dir = _state_dir()

    async def run() -> None:
        proxy = StatelessDaemonProxy(UnixDaemonConnectionFactory(_socket_path()))
        await serve_channel_stdio(
            proxy,
            actor=args.actor,
            session_ref=args.session_ref,
            cwd=str(_resolved_agent_cwd(args.actor, args.cwd)),
            command=tuple(args.command or ("claude",)),
            logger=Logger.worker(state_dir, runtime="claude", name=args.actor),
            poll_interval=args.poll_interval,
            recovery_signal=args.recovery_signal,
            turn_signal_dir=args.turn_signal_dir,
            owner_pid=args.owner_pid,
            owner_identity=args.owner_identity,
            tmux_session=args.tmux_session,
        )

    # stdout belongs exclusively to the MCP stdio wire.
    anyio.run(run)
    return 0


def _run_claude_channel_recover(args: argparse.Namespace) -> int:
    from hyprial.kernel import require_initialized_hyprial_home

    require_initialized_hyprial_home()

    from hyprial.daemon import signal_channel_recovery

    signal_channel_recovery(args.signal_path.expanduser().resolve())
    return 0


class SessionRefFileProxy:
    """Sign calls with the sessionRef published after launch."""

    def __init__(self, inner: Any, path: Path, placeholder: str) -> None:
        self._inner, self._path, self._placeholder = inner, path, placeholder

    async def call(self, **kwargs: Any) -> Any:
        try:
            kwargs["session_ref"] = self._path.read_text().strip() or self._placeholder
        except OSError:
            kwargs["session_ref"] = self._placeholder
        return await self._inner.call(**kwargs)


def _run_agent_channel(args: argparse.Namespace) -> int:
    from hyprial.kernel import require_initialized_hyprial_home

    require_initialized_hyprial_home()

    session_ref = args.session_ref or os.environ.get("HYPRIAL_WORKER_SESSION_REF")
    if args.session_ref_file is not None:
        session_ref = session_ref or "pending"
    if not session_ref:
        raise SystemExit(
            "managed worker channel requires HYPRIAL_WORKER_SESSION_REF"
        )

    import anyio

    from hyprial.daemon import (
        StatelessDaemonProxy,
        UnixDaemonConnectionFactory,
        serve_worker_stdio,
    )

    async def run() -> None:
        proxy: Any = StatelessDaemonProxy(
            UnixDaemonConnectionFactory(_socket_path())
        )
        if args.session_ref_file is not None:
            proxy = SessionRefFileProxy(proxy, args.session_ref_file, session_ref)
        await serve_worker_stdio(proxy, actor=args.actor, session_ref=session_ref)

    # stdout belongs exclusively to the MCP stdio wire.
    anyio.run(run)
    return 0


def _install_carrier_signal_handlers(stop_event: threading.Event) -> dict[int, Any]:
    """SIGTERM/SIGHUP stop the carrier; SIGINT belongs to the TUI child."""

    return {
        signal.SIGTERM: signal.signal(
            signal.SIGTERM, lambda *_args: stop_event.set()
        ),
        signal.SIGHUP: signal.signal(signal.SIGHUP, lambda *_args: stop_event.set()),
        signal.SIGINT: signal.signal(signal.SIGINT, signal.SIG_IGN),
    }


def _restore_carrier_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _spawn_owned_tui(tui_argv: list[str], *, cwd: str, launch_dir: Path) -> Any:
    """Start the TUI on the terminal, then move carrier stderr off its screen."""

    process = subprocess.Popen(tui_argv, cwd=cwd)
    path = launch_dir / DSH_CARRIER_STDERR_FILENAME
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        sys.stderr.flush()
        os.dup2(descriptor, 2)
    finally:
        os.close(descriptor)
    return process


def _run_dsh_carrier(args: argparse.Namespace) -> int:
    """Own one dsh-tui launch: spawn, register, wake, and unregister."""

    from hyprial.kernel import Logger, require_initialized_hyprial_home

    require_initialized_hyprial_home()

    import anyio

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
        tui_argv = json.loads(args.tui_argv)
    except ValueError as error:
        raise SystemExit(f"--tui-argv is not valid JSON: {error}") from error
    if (
        not isinstance(tui_argv, list)
        or not tui_argv
        or not all(isinstance(item, str) for item in tui_argv)
    ):
        raise SystemExit("--tui-argv must be a non-empty JSON array of strings")

    resolved_cwd = str(_resolved_agent_cwd(args.actor, args.cwd))
    try:
        tui_process = _spawn_owned_tui(
            tui_argv, cwd=resolved_cwd, launch_dir=args.launch_dir
        )
    except OSError as error:
        write_launch_state(args.launch_dir, DSH_TUI_SPAWN_ERROR_FILENAME, str(error))
        raise SystemExit(f"failed to start dsh-tui: {error}") from error

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
            tui_process.wait(timeout=5.0)
        message = (
            "cannot read a birth identity for the dsh-tui process"
            f" (pid {tui_process.pid})"
        )
        write_launch_state(args.launch_dir, DSH_TUI_SPAWN_ERROR_FILENAME, message)
        raise SystemExit(message)

    write_launch_state(args.launch_dir, DSH_TUI_PID_FILENAME, str(tui_process.pid))
    write_launch_state(args.launch_dir, DSH_TUI_IDENTITY_FILENAME, identity)
    stop_event = threading.Event()

    def watch_tui() -> None:
        while tui_process.poll() is None:
            time.sleep(0.1)
        stop_event.set()

    tui_watcher = threading.Thread(
        target=watch_tui, name=f"hyprial-dsh-tui-watch-{args.actor}", daemon=True
    )
    tui_watcher.start()

    async def run() -> None:
        proxy = StatelessDaemonProxy(UnixDaemonConnectionFactory(_socket_path()))
        await run_dsh_carrier(
            proxy,
            actor=args.actor,
            session_ref=args.session_ref,
            cwd=resolved_cwd,
            command=("dsh-tui",),
            control_socket=args.control_socket,
            tui_pid=tui_process.pid,
            tui_identity=identity,
            logger=Logger.worker(_state_dir(), runtime="dsh", name=args.actor),
            poll_interval=args.poll_interval,
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
            tui_process.wait(timeout=5.0)
        tui_watcher.join(timeout=2.0)
        write_launch_state(
            args.launch_dir,
            DSH_TUI_EXIT_CODE_FILENAME,
            str(tui_process.returncode),
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hyprial.daemon.impl.mcp.entry",
        description="Run one local MCP channel server (internal process entry).",
    )
    entries = parser.add_subparsers(dest="entry", required=True)

    claude = entries.add_parser(
        "claude-channel", help="Run one Claude Code-owned stdio Channel server."
    )
    claude.add_argument("--actor", required=True)
    claude.add_argument("--session-ref", required=True)
    claude.add_argument("--cwd", type=Path)
    claude.add_argument("--command", action="append")
    claude.add_argument("--poll-interval", type=float, default=0.5)
    claude.add_argument("--recovery-signal", type=Path)
    claude.add_argument("--turn-signal-dir", type=Path)
    claude.add_argument("--owner-pid", type=int)
    claude.add_argument("--owner-identity")
    claude.add_argument("--tmux-session")
    claude.set_defaults(handler=_run_claude_channel)

    recover = entries.add_parser(
        "claude-channel-recover",
        help="Pulse one running Claude Channel after a lifecycle/model failure.",
    )
    recover.add_argument("--signal-path", type=Path, required=True)
    recover.set_defaults(handler=_run_claude_channel_recover)

    agent = entries.add_parser(
        "agent-channel", help="Run one managed-worker stdio MCP server."
    )
    agent.add_argument("--actor", required=True)
    agent.add_argument("--session-ref", help=argparse.SUPPRESS)
    agent.add_argument("--session-ref-file", type=Path, help=argparse.SUPPRESS)
    agent.set_defaults(handler=_run_agent_channel)

    dsh = entries.add_parser(
        "dsh-carrier", help="Run one launcher-owned dsh-tui carrier."
    )
    dsh.add_argument("--actor", required=True)
    dsh.add_argument("--session-ref", required=True)
    dsh.add_argument("--cwd", type=Path, required=True)
    dsh.add_argument("--control-socket", type=Path, required=True)
    dsh.add_argument("--launch-dir", type=Path, required=True)
    dsh.add_argument("--tui-argv", required=True)
    dsh.add_argument("--poll-interval", type=float, default=0.5)
    dsh.set_defaults(handler=_run_dsh_carrier)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
