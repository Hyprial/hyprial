"""Executable adapters and additive fields for public wire contracts.

Keep the executable ``wire`` adapter lazy: actor-owned domain modules import
their frozen ``contracts.ports`` types while the inbox package is still being
initialized.  Eagerly importing ``wire`` here would re-enter ``hyprial.inbox`` and
make otherwise independent public modules depend on package import order.
"""

from typing import Any

from .channel import (
    CHANNEL_PROTOCOL_VERSION,
    channel_generation,
    safe_channel_build_version,
)
from .lark import lark_recovery_coverage
from .session import (
    SESSION_CARRIER_SOURCES,
    SESSION_FETCH_PARAM,
    is_session_fetch,
    session_fetch_params,
)


def __getattr__(name: str) -> Any:
    if name == "evaluate_case":
        from .wire import evaluate_case

        return evaluate_case
    raise AttributeError(name)

__all__ = [
    "CHANNEL_PROTOCOL_VERSION",
    "SESSION_CARRIER_SOURCES",
    "SESSION_FETCH_PARAM",
    "channel_generation",
    "evaluate_case",
    "is_session_fetch",
    "lark_recovery_coverage",
    "safe_channel_build_version",
    "session_fetch_params",
]
