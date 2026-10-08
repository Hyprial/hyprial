"""Pure additive fields for public wire contracts.

The executable wire adapter is owned by the daemon domain.
"""

from hyprial.kernel.impl.contracts.channels.channel import (
    CHANNEL_PROTOCOL_VERSION,
    channel_generation,
    safe_channel_build_version,
)
from hyprial.kernel.impl.contracts.channels.lark import lark_recovery_coverage
from hyprial.kernel.impl.contracts.execution.session import (
    SESSION_CARRIER_SOURCES,
    SESSION_FETCH_PARAM,
    is_session_fetch,
    session_fetch_params,
)


__all__ = [
    "CHANNEL_PROTOCOL_VERSION",
    "SESSION_CARRIER_SOURCES",
    "SESSION_FETCH_PARAM",
    "channel_generation",
    "is_session_fetch",
    "lark_recovery_coverage",
    "safe_channel_build_version",
    "session_fetch_params",
]
