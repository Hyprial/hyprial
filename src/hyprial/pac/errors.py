"""Error surface for PAC v2 (graph file + flag reactor).

These are PAC-domain codes: they surface in ``hyprial pac`` ``--json`` output
and in exit statuses.  Since the principal-URI write boundary (2026-09-14,
G1=A) the daemon's ``pac.*`` IPC methods also raise them -- the handler
translates :class:`PacError` into ``DaemonRequestError`` with the SAME code,
verbatim, so a caller sees one code space whether the write rode the local
(human) path or the fenced (agent) path.  They keep living in this module
rather than ``contracts.ipc_errors`` by the same precedent as
``WorkflowServiceError``/``AgentTaskError`` domain codes: the daemon passes
them through without branching on them, and this module is the one
definition site both sides import.
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

#: Principal (owner/creator) grammar: the value is neither a canonical
#: ``user:<owner>`` nor a canonical ``agent:<owner>:<machine>:<actor>`` URI,
#: or it carries whitespace/control characters the PAC boundary forbids.
PAC_PRINCIPAL_SHAPE_INVALID = "PAC_PRINCIPAL_SHAPE_INVALID"

#: Authoring-time resolution: a short name matched more than one candidate
#: (or one candidate that cannot be confirmed within a complete scope) --
#: the caller must resubmit with the full URI; nothing was written.
PAC_OWNER_AMBIGUOUS = "PAC_OWNER_AMBIGUOUS"

#: Authoring-time resolution: a short name matched zero candidates.
PAC_OWNER_UNRESOLVED = "PAC_OWNER_UNRESOLVED"

#: Authoring-time resolution: a candidate source failed or its coverage is
#: unknown -- "we could not look everywhere" is never folded into "unique".
PAC_RESOLUTION_UNAVAILABLE = "PAC_RESOLUTION_UNAVAILABLE"

#: The acting principal could not be verified: no daemon-bound session for
#: an agent caller, or a presented ``--actor`` that disagrees with the
#: verified identity.  ``--actor`` is a claim, never a credential.
PAC_PRINCIPAL_UNVERIFIED = "PAC_PRINCIPAL_UNVERIFIED"

#: An operation-key replay presented a different creating principal than the
#: graph the key already minted -- the replay is refused, not silently
#: re-bound and never answered with a second graph.
PAC_OPERATION_KEY_CONFLICT = "PAC_OPERATION_KEY_CONFLICT"

#: The daemon answered a pac.* call with a malformed result object.
PAC_IPC_INVALID_RESPONSE = "PAC_IPC_INVALID_RESPONSE"

#: The schema-8 owner-rewrite migration could not read a resolution source
#: (corrupt agents registry / malformed user profiles).  The migration
#: aborts as a whole -- zero writes -- rather than rewriting half-blind.
PAC_MIGRATION_SOURCE_UNREADABLE = "PAC_MIGRATION_SOURCE_UNREADABLE"


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
    "PAC_IPC_INVALID_RESPONSE",
    "PAC_MIGRATION_SOURCE_UNREADABLE",
    "PAC_OPERATION_KEY_CONFLICT",
    "PAC_OWNER_AMBIGUOUS",
    "PAC_OWNER_UNRESOLVED",
    "PAC_PRINCIPAL_SHAPE_INVALID",
    "PAC_PRINCIPAL_UNVERIFIED",
    "PAC_RESOLUTION_UNAVAILABLE",
    "PAC_OWNER_UNKNOWN",
    "PacError",
]
