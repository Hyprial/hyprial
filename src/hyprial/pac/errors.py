"""Error surface for PAC v2 (graph file + flag reactor).

These are CLI-level codes: they surface in ``hyprial pac`` ``--json`` output and
in exit statuses, the same display-only shape ``WORKFLOW_SCHEMA_ERROR`` has
outside the frozen daemon-IPC registry.  None of them crosses the daemon
IPC error channel (the pac commands talk to their own SQLite file and, for
notification delivery, the public ``message.send`` seam), so per the
membership line in :mod:`hyprial.contracts.ipc_errors` they stay OUT of that
registry until a second process branches on one.
"""

from __future__ import annotations

from typing import Any

#: Edit validation: the CAS version the caller presented is not current.
#: The spec names this code verbatim (spec-pac-v2-slice-1 ① / phases P0-2).
PAC_GRAPH_VERSION_CONFLICT = "PAC_GRAPH_VERSION_CONFLICT"

#: Edit validation: a forward edge would close an implicit cycle.
PAC_GRAPH_FORWARD_CYCLE = "PAC_GRAPH_FORWARD_CYCLE"

#: Edit validation: the node's owner is neither an owner already on this
#: graph nor a known principal/agent short name.
PAC_OWNER_UNKNOWN = "PAC_OWNER_UNKNOWN"

#: Shape rejections for node/edge arguments.
PAC_NODE_NOT_FOUND = "PAC_NODE_NOT_FOUND"
PAC_NODE_EXISTS = "PAC_NODE_EXISTS"
PAC_EDGE_EXISTS = "PAC_EDGE_EXISTS"
PAC_EDGE_INVALID = "PAC_EDGE_INVALID"
PAC_NODE_SHAPE_INVALID = "PAC_NODE_SHAPE_INVALID"
PAC_GRAPH_NOT_FOUND = "PAC_GRAPH_NOT_FOUND"
PAC_GRAPH_EXISTS = "PAC_GRAPH_EXISTS"
PAC_GRAPH_FROZEN = "PAC_GRAPH_FROZEN"
PAC_GRAPH_NOT_ACTIVE = "PAC_GRAPH_NOT_ACTIVE"
PAC_GRAPH_CLOSED = "PAC_GRAPH_CLOSED"
PAC_GRAPH_NOT_OWNER = "PAC_GRAPH_NOT_OWNER"
PAC_EVENTS_RESYNC = "PAC_EVENTS_RESYNC"
PAC_EVENT_TYPE_UNKNOWN = "PAC_EVENT_TYPE_UNKNOWN"
PAC_EVENT_ENVELOPE_INVALID = "PAC_EVENT_ENVELOPE_INVALID"

#: Flag semantics (concept §3, §7-D: an owner only flips their own node).
PAC_FLAG_NOT_OWNER = "PAC_FLAG_NOT_OWNER"
PAC_FLAG_ALREADY_SET = "PAC_FLAG_ALREADY_SET"
PAC_FLAG_NOT_SET = "PAC_FLAG_NOT_SET"

#: Notification delivery could not reach the daemon; the rows are recorded
#: and ``hyprial pac notify resend`` retries exactly the undelivered ones.
PAC_NOTIFY_DELIVERY_FAILED = "PAC_NOTIFY_DELIVERY_FAILED"


class PacError(RuntimeError):
    """One PAC failure carrying its stable CLI-level code and payload."""

    def __init__(self, code: str, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


__all__ = [
    "PAC_EDGE_EXISTS",
    "PAC_EDGE_INVALID",
    "PAC_FLAG_ALREADY_SET",
    "PAC_FLAG_NOT_OWNER",
    "PAC_FLAG_NOT_SET",
    "PAC_EVENTS_RESYNC",
    "PAC_EVENT_TYPE_UNKNOWN",
    "PAC_EVENT_ENVELOPE_INVALID",
    "PAC_GRAPH_CLOSED",
    "PAC_GRAPH_EXISTS",
    "PAC_GRAPH_FROZEN",
    "PAC_GRAPH_NOT_ACTIVE",
    "PAC_GRAPH_NOT_OWNER",
    "PAC_GRAPH_FORWARD_CYCLE",
    "PAC_GRAPH_NOT_FOUND",
    "PAC_GRAPH_VERSION_CONFLICT",
    "PAC_NODE_EXISTS",
    "PAC_NODE_NOT_FOUND",
    "PAC_NODE_SHAPE_INVALID",
    "PAC_NOTIFY_DELIVERY_FAILED",
    "PAC_OWNER_UNKNOWN",
    "PacError",
]
