"""Additive, non-secret telemetry contract for interactive MCP Channels."""

from __future__ import annotations

import re

# Increment only when the registration/status telemetry contract changes in a
# way that operators must distinguish from an already-running stdio child.
CHANNEL_PROTOCOL_VERSION = 2
# Heartbeat cadence is deliberately independent from message polling and stdio
# notification delivery. The same-OS-user boundary is trusted: this lease is an
# operational misuse fence, not a hostile-local authentication mechanism.
CHANNEL_HEARTBEAT_INTERVAL_SECONDS = 1.0
CHANNEL_HEARTBEAT_MISS_BUDGET = 6
CHANNEL_LIVENESS_TTL_SECONDS = (
    CHANNEL_HEARTBEAT_INTERVAL_SECONDS * CHANNEL_HEARTBEAT_MISS_BUDGET
)
_SAFE_BUILD_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,63}\Z")


def safe_channel_build_version(value: object) -> bool:
    """Accept only short package-version tokens suitable for status output."""

    return isinstance(value, str) and _SAFE_BUILD_VERSION.fullmatch(value) is not None


def channel_generation(
    *, build_version: object, protocol_version: object
) -> str:
    """Classify code generation without claiming the process is live/healthy."""

    if (
        safe_channel_build_version(build_version)
        and isinstance(protocol_version, int)
        and not isinstance(protocol_version, bool)
        and protocol_version == CHANNEL_PROTOCOL_VERSION
    ):
        return "current"
    return "legacy_or_unknown"


__all__ = [
    "CHANNEL_PROTOCOL_VERSION",
    "CHANNEL_HEARTBEAT_INTERVAL_SECONDS",
    "CHANNEL_HEARTBEAT_MISS_BUDGET",
    "CHANNEL_LIVENESS_TTL_SECONDS",
    "channel_generation",
    "safe_channel_build_version",
]
