"""Channel session entry points (stdio serving and full session loop)."""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from mcp import types
from mcp.server.runner import serve_loop
from mcp.server.stdio import stdio_server

from hyprial.daemon.impl.desired_state import DesiredState, InteractiveSession
from hyprial.kernel import Logger

from hyprial.daemon.impl.mcp.proxy  import StatelessDaemonProxy
from hyprial.daemon.impl.mcp.wake  import WakeCoordinator
from hyprial.daemon.impl.mcp.channel.adapter import (
    ClaudeChannelAdapter,
    RuntimeKind,
    _install_legacy_discover_handler,
    channel_initialization_options,
)
from hyprial.daemon.impl.mcp.channel.loops import (
    _run_channel_daemon_loops,
    _watch_parent,
)
from hyprial.daemon.impl.mcp.channel.notifier import (
    StdioChannelNotifier,
)
from hyprial.daemon.impl.mcp.channel.ownership import (
    _DAEMON_CONTACT_ERRORS,
    _resolve_tmux_pane_owner,
)
from hyprial.daemon.impl.mcp.channel.notifier import ClaudeChannelDriver

_logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class _SessionStore:
    state: DesiredState

    def load(self) -> DesiredState:
        return self.state

async def serve_handshake_era(
    lowlevel: Any,
    read_stream: Any,
    write_stream: Any,
    init_options: Any,
) -> None:
    """Serve the channel on the handshake (2025) protocol era only.

    The channel lives on unsolicited ``notifications/claude/channel`` pushes,
    which only a handshake-era session carries. ``Server.run`` drives the
    dual-era loop, which locks a connection to the 2026-07-28 era as soon as
    the client's first request carries the modern ``_meta`` envelope. Claude
    Code 2.1.286+ opens with exactly such a ``server/discover``, reads our
    handshake-only versions, and falls back to ``initialize`` on the same
    connection, which a modern-locked connection refuses (-32022), so the
    channel never came up. ``serve_loop`` serves the handshake era alone, so
    that fallback succeeds.
    """

    async with lowlevel.lifespan(lowlevel) as lifespan_state:
        await serve_loop(
            lowlevel,
            read_stream,
            write_stream,
            lifespan_state=lifespan_state,
            init_options=init_options,
        )


async def run_channel_session(
    read_stream: Any,
    write_stream: Any,
    proxy: StatelessDaemonProxy,
    *,
    actor: str,
    session_ref: str,
    cwd: str,
    command: tuple[str, ...],
    logger: Logger | None = None,
    poll_interval: float = 0.5,
    recovery_signal: Path | None = None,
    turn_signal_dir: Path | None = None,
    on_error: Callable[[BaseException, int], None] | None = None,
    getppid: Callable[[], int] | None = None,
    owner_pid: int | None = None,
    owner_identity: str | None = None,
    parent_poll_interval: float = 1.0,
    on_parent_death: Callable[[], None] | None = None,
    tmux_session: str | None = None,
) -> None:
    """Run the channel MCP server, its resilient poll loop, and a parent watch.

    Factored out of ``serve_channel_stdio`` so the exact production wiring -- the
    same server, coordinator, adapter and poll loop -- can be driven over
    in-memory streams in tests, including a daemon restart mid-session and a
    parent-death teardown (``getppid``/``on_parent_death`` are injectable for
    that; production defaults to real ``os.getppid`` and ``os._exit``).

    Three concurrent concerns, deliberately independent:

    * ``run_server`` -- the stdio MCP server; its return (a clean stream close)
      tears the session down.
    * ``poll_daemon`` -- the daemon poll loop; it may go quiet on a supersede
      verdict but never tears the session down itself.
    * ``watch_parent`` -- reaps the child when its managed launch owner (or the
      direct parent on the compatibility path) dies. It keys on process
      liveness so it can never fire while that owner is alive. On owner death it
      best-effort unregisters and then
      *force-exits the process*: cancelling the task group is not enough because
      the mcp stdio reader thread can stay blocked on a pipe that never sees EOF
      (an inherited write-end), keeping the process alive indefinitely.
    """

    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    if parent_poll_interval <= 0:
        raise ValueError("parent_poll_interval must be positive")
    if owner_pid is None and tmux_session is not None:
        # Detached-tmux launches outlive their launcher, so the launcher PID
        # can never be the owner fence here. The pane's top process lives
        # exactly as long as the tmux session does; when the lookup fails the
        # getppid compatibility watch below still applies.
        resolved_owner = _resolve_tmux_pane_owner(tmux_session)
        if resolved_owner is not None:
            owner_pid, owner_identity = resolved_owner
    resolved_getppid = os.getppid if getppid is None else getppid
    resolved_on_parent_death = (
        (lambda: os._exit(0)) if on_parent_death is None else on_parent_death
    )
    session = InteractiveSession(
        actor=actor,
        cwd=cwd,
        command=command,
        source="claude-channel",
        session_ref=session_ref,
        runtime=RuntimeKind.CLAUDE_INTERACTIVE,
        channel_confirmed=True,
    )
    coordinator = WakeCoordinator(
        _SessionStore(DesiredState(interactive_sessions=(session,))),
        ClaudeChannelDriver(StdioChannelNotifier(write_stream)),
        logger=logger,
    )
    adapter = ClaudeChannelAdapter(
        proxy,
        coordinator,
        actor=actor,
        session_ref=session_ref,
        cwd=cwd,
        command=command,
        owner_fence=owner_pid is not None and owner_identity is not None,
        tmux_session=tmux_session,
    )
    # Import lazily to keep the server's TYPE_CHECKING-only adapter edge and
    # avoid a module initialization cycle.
    from hyprial.daemon.impl.mcp.server  import create_channel_mcp_server

    mcp_server = create_channel_mcp_server(adapter)
    lowlevel = mcp_server._lowlevel_server
    _install_legacy_discover_handler(lowlevel)
    initialized = anyio.Event()

    async def on_initialized(_ctx: Any, _params: types.NotificationParams) -> None:
        initialized.set()

    lowlevel.add_notification_handler(
        "notifications/initialized", types.NotificationParams, on_initialized
    )

    async with anyio.create_task_group() as tasks:

        async def run_server() -> None:
            try:
                await serve_handshake_era(
                    lowlevel,
                    read_stream,
                    write_stream,
                    channel_initialization_options(lowlevel),
                )
            finally:
                tasks.cancel_scope.cancel()

        async def poll_daemon() -> None:
            await initialized.wait()
            try:
                await _run_channel_daemon_loops(
                    adapter,
                    poll_interval=poll_interval,
                    recovery_signal=recovery_signal,
                    turn_signal_dir=turn_signal_dir,
                    on_error=on_error,
                )
            finally:
                # Best-effort daemon-side cleanup; a daemon that is down at
                # shutdown must not turn teardown into a crash. A superseded child
                # unregisters no-op (the fence is ref-guarded), so this never
                # clobbers the new owner's registration.
                with anyio.CancelScope(shield=True):
                    with anyio.move_on_after(1.0):
                        try:
                            await adapter.stop()
                        except _DAEMON_CONTACT_ERRORS:
                            pass

        async def watch_parent() -> None:
            orphaned = await _watch_parent(
                getppid=resolved_getppid,
                poll_interval=parent_poll_interval,
                owner_pid=owner_pid,
                owner_identity=owner_identity,
            )
            if not orphaned:
                return
            # Best-effort: drop this session's registration so the daemon does not
            # keep routing to a dead child, then force-exit. adapter.stop() is
            # ref-guarded on the daemon side, so an already-superseded child's
            # unregister is a no-op and never clobbers the new owner.
            with anyio.CancelScope(shield=True):
                with anyio.move_on_after(1.0):
                    try:
                        await adapter.stop()
                    except _DAEMON_CONTACT_ERRORS:
                        pass
            resolved_on_parent_death()

        tasks.start_soon(run_server)
        tasks.start_soon(poll_daemon)
        tasks.start_soon(watch_parent)

async def serve_channel_stdio(
    proxy: StatelessDaemonProxy,
    *,
    actor: str,
    session_ref: str,
    cwd: str,
    command: tuple[str, ...],
    logger: Logger | None = None,
    poll_interval: float = 0.5,
    recovery_signal: Path | None = None,
    turn_signal_dir: Path | None = None,
    owner_pid: int | None = None,
    owner_identity: str | None = None,
    tmux_session: str | None = None,
) -> None:
    """Run one CC-owned stdio MCP child and poll daemon-authoritative pending work.

    A daemon restart (the deploy path) drops the child's Unix socket to the
    daemon but never the Claude Code <-> child stdio transport, so the child
    stays alive, reconnects to the new daemon and re-registers -- no Claude Code
    session restart or ``/mcp reconnect`` required. The transport stays stdio
    deliberately: Claude Code channels (the wake edge) are stdio-only, so moving
    the coordinator channel to HTTP would silently delete wake.
    """

    async with stdio_server() as (read_stream, write_stream):
        await run_channel_session(
            read_stream,
            write_stream,
            proxy,
            actor=actor,
            session_ref=session_ref,
            cwd=cwd,
            command=command,
            logger=logger,
            poll_interval=poll_interval,
            recovery_signal=recovery_signal,
            turn_signal_dir=turn_signal_dir,
            owner_pid=owner_pid,
            owner_identity=owner_identity,
            tmux_session=tmux_session,
        )
