"""Agent teleport slice 4b: the target-side service, its ports and the query binding."""

from hyprial.daemon.impl.teleport.target.ports import Landed, TargetHost, TargetRoutines
from hyprial.daemon.impl.teleport.target.query import bind_target_service, handle_query
from hyprial.daemon.impl.teleport.target.service import TeleportTargetService

__all__ = [
    "Landed",
    "TargetHost",
    "TargetRoutines",
    "TeleportTargetService",
    "bind_target_service",
    "handle_query",
]
