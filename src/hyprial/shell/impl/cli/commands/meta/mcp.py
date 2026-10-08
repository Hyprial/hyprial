"""``hyprial mcp`` local MCP adapter channels."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.kernel import Logger
from pathlib import Path
from hyprial.kernel import ipc_errors
import os
import typer

from hyprial.shell.impl.cli.commands.common.support import _resolved_agent_cwd
mcp_app = typer.Typer(help="Run local MCP adapters.")


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


@mcp_app.command("agent-channel")
def mcp_agent_channel(
    actor: str = typer.Option(..., "--actor"),
    session_ref: str | None = typer.Option(None, "--session-ref", hidden=True),
) -> None:
    """Run one managed-worker stdio MCP server with a fixed daemon identity."""
    services = get_services()

    services.require_initialized_hyprial_home()
    session_ref = session_ref or os.environ.get("HYPRIAL_WORKER_SESSION_REF")
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
        await serve_worker_stdio(proxy, actor=actor, session_ref=session_ref)

    # stdout belongs exclusively to the MCP stdio wire.
    anyio.run(run)
