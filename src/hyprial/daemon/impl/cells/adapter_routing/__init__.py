"""Read-only reverse index from adapter name to agent cell (I6-3)."""

from hyprial.daemon.impl.cells.adapter_routing.index import (
    agent_adapter_bindings,
    AdapterRoutingConflict,
    AdapterRoutingIndex,
    AdapterRoutingResolution,
    AdapterRoutingTarget,
    build_adapter_routing_index,
)

__all__ = [
    "agent_adapter_bindings",
    "AdapterRoutingConflict",
    "AdapterRoutingIndex",
    "AdapterRoutingResolution",
    "AdapterRoutingTarget",
    "build_adapter_routing_index",
]
