"""Module-private process entries for the local MCP channel servers.

The interactive launcher and the daemon spawn these stdio servers directly, so
R11 moved them out of the ``hyprial`` CLI tree: there is no ``mcp`` command and
no public spelling.  The argv below is still an internal contract between the
spawn site and this process, and each subcommand mirrors exactly the flags the
launcher/daemon writes::

    python -m hyprial.daemon.impl.mcp.entry claude-channel --actor A --session-ref S ...
    python -m hyprial.daemon.impl.mcp.entry claude-channel-recover --signal-path P
    python -m hyprial.daemon.impl.mcp.entry agent-channel --actor A

``claude-channel-recover`` stays here (not in a stdlib-only module) because it
is the SessionStart/StopFailure hook the launcher embeds in ``--settings`` for
an already-running session; it must keep the same startup shape as the server
it pulses.  ``mcp claude-turn-ended`` was deleted: its Stop-hook job is owned
by the stdlib ``hyprial.biz.impl.turn_pulse`` entry.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any


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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
