"""Local hc JSON-RPC client interfaces."""

from .events import HcEventSubscription
from .protocol import (
    CoreIdentity,
    CoreStatus,
    HcHeartbeatTimeout,
    HcRpcError,
    HcStreamClosed,
    HcStreamEvent,
    HcTransportError,
    HelloResult,
    HolderInfo,
    SubscribeSnapshot,
    TerminalRecord,
    TerminalStatus,
)
from .rpc import HcClient, probe_socket_alive

__all__ = [
    "CoreIdentity",
    "CoreStatus",
    "HcClient",
    "HcEventSubscription",
    "HcHeartbeatTimeout",
    "HcRpcError",
    "HcStreamClosed",
    "HcStreamEvent",
    "HcTransportError",
    "HelloResult",
    "HolderInfo",
    "SubscribeSnapshot",
    "TerminalRecord",
    "TerminalStatus",
    "probe_socket_alive",
]
