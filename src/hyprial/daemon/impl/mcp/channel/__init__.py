"""MCP channel edge (semantic re-export root; keeps mcp.channel path)."""

from hyprial.daemon.impl.mcp.channel.adapter import (
    RuntimeKind,
    ClaudeChannelAdapter,
    _install_legacy_discover_handler,
    channel_initialization_options,
)
from hyprial.daemon.impl.mcp.channel.loops import (
    signal_channel_recovery,
    _run_channel_daemon_loops,
    _run_channel_heartbeat_loop,
    _run_channel_poll_loop,
    _watch_parent,
)
from hyprial.daemon.impl.mcp.channel.notifier import (
    CHANNEL_CAPABILITY,
    CHANNEL_NOTIFICATION,
    ChannelNotifier,
    ClaudeChannelDriver,
    StdioChannelNotifier,
)
from hyprial.daemon.impl.mcp.channel.ownership import (
    _OwnerProcessStatus,
    _identity_components,
    _owner_process_alive,
    _owner_process_status,
    _read_process_identity,
)
from hyprial.daemon.impl.mcp.channel.session import (
    run_channel_session,
    serve_channel_stdio,
)

__all__ = [
    "CHANNEL_CAPABILITY",
    "CHANNEL_NOTIFICATION",
    "ChannelNotifier",
    "ClaudeChannelAdapter",
    "ClaudeChannelDriver",
    "RuntimeKind",
    "StdioChannelNotifier",
    "_OwnerProcessStatus",
    "_identity_components",
    "_owner_process_alive",
    "_owner_process_status",
    "_read_process_identity",
    "_run_channel_daemon_loops",
    "_run_channel_heartbeat_loop",
    "_run_channel_poll_loop",
    "_watch_parent",
    "signal_channel_recovery",
    "_install_legacy_discover_handler",
    "channel_initialization_options",
    "run_channel_session",
    "serve_channel_stdio",
]
