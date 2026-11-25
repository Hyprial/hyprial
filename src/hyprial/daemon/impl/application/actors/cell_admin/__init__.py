"""Unregistered cell editing core service (I6-3)."""

from hyprial.daemon.impl.application.actors.cell_admin.service import (
    ALLOWED_UPDATE_PARAMS,
    FORBIDDEN_CELL_FIELDS,
    get_agent_cell,
    update_agent_cell,
)

__all__ = [
    "ALLOWED_UPDATE_PARAMS",
    "FORBIDDEN_CELL_FIELDS",
    "get_agent_cell",
    "update_agent_cell",
]
