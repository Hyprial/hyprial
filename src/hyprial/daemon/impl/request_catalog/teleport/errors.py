# Decision record (file header; no inline comments by repository rule):
# - ``teleport.run`` declares every frozen ``TELEPORT_*`` code (the list comes
#   from the teleport package so a new code cannot be forgotten here) plus the
#   codes the daemon already owns: ``INVALID_ARGUMENT``, ``AGENT_NOT_FOUND``
#   and ``USER_AGENT_IMMUTABLE`` (the owner's user agent never moves, UA1g).
# - Only two outcomes carry data, because they need a manual follow-up:
#   ``TELEPORT_UNRESOLVED`` (the source agent stays stopped, the target may
#   hold the agent) and ``TELEPORT_SOURCE_CLEANUP_FAILED`` (the target runs the
#   agent, the stopped source copy is still here).  Both schemas are closed.
"""The declared errors of ``teleport.run``."""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.teleport.errors import TELEPORT_ERROR_CODES
from hyprial.kernel import MethodError, ipc_errors

_STRING = {"type": "string", "minLength": 1}
_STRINGS = {"type": "array", "items": _STRING}

UNRESOLVED_DATA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operationId": _STRING,
        "targetNode": _STRING,
        "agent": _STRING,
        "sourceAgentUri": _STRING,
        "bytesSent": {"type": "integer", "minimum": 0},
        "sourceState": {"const": "stopped"},
        "pausedRoutines": _STRINGS,
        "manualCheck": _STRING,
    },
    "required": [
        "operationId",
        "targetNode",
        "agent",
        "sourceAgentUri",
        "bytesSent",
        "sourceState",
        "pausedRoutines",
        "manualCheck",
    ],
    "additionalProperties": False,
}

CLEANUP_FAILED_DATA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operationId": _STRING,
        "agent": _STRING,
        "targetNode": _STRING,
        "targetAgentUri": _STRING,
        "pausedRoutines": _STRINGS,
    },
    "required": [
        "operationId",
        "agent",
        "targetNode",
        "targetAgentUri",
        "pausedRoutines",
    ],
    "additionalProperties": False,
}

_DESCRIPTIONS = {
    "TELEPORT_BAD_FRAME": "A peer answered with a frame this node cannot read; the source was restored.",
    "TELEPORT_WRONG_TARGET": "The target is this node, or no node answered the prepare request; nothing changed.",
    "TELEPORT_UNKNOWN_OPERATION": "The target no longer knows the operation (it restarted); the source was restored.",
    "TELEPORT_OPERATION_CONFLICT": "Another teleport of this agent is running here, or the target already holds this operation.",
    "TELEPORT_OFFSET_GAP": "The target's stream position disagreed with the sender; the source was restored.",
    "TELEPORT_CHUNK_TOO_LARGE": "The target refused a chunk as too large; the source was restored.",
    "TELEPORT_CHUNK_DIGEST_MISMATCH": "A chunk arrived damaged; the source was restored.",
    "TELEPORT_STREAM_MISMATCH": "The stream sent differs from the bundle exported; the source was restored.",
    "TELEPORT_INCOMPLETE": "The target received too few bytes or stopped acknowledging; the source was restored.",
    "TELEPORT_DIGEST_MISMATCH": "The target found the bundle damaged or inconsistent; the source was restored.",
    "TELEPORT_AGENT_EXISTS": "The target already has an agent with this name; the source is unchanged.",
    "TELEPORT_ROUTINE_CONFLICT": "The target already has a routine with a migrating name; the source is unchanged.",
    "TELEPORT_RUNTIME_UNSUPPORTED": "The agent or the target cannot take part in a teleport (no home, not headless, unsupported harness, pinned adapter, cwd outside the home); nothing changed.",
    "TELEPORT_OWNER_MISMATCH": "The target daemon belongs to another owner; the source is unchanged.",
    "TELEPORT_STRICT_RESUME_FAILED": "The target could not resume the session; the target was cleaned up and the source restored.",
    "TELEPORT_IN_FLIGHT_TIMEOUT": "A routine of the agent still had work in flight; the source was restored.",
    "TELEPORT_SOURCE_CLEANUP_FAILED": "The agent runs on the target but the stopped source copy could not be removed; the target routines stay paused.",
    "TELEPORT_UNRESOLVED": "The target gave no readable answer to receive; the source agent stays stopped and the operator must check the target.",
    "TELEPORT_LANDING_FAILED": "The target could not land or activate the agent; the target was cleaned up and the source restored.",
    "TELEPORT_SOURCE_NOT_QUIESCED": "The source agent could not be stopped or exported; it was restarted.",
}

_DATA = {
    "TELEPORT_UNRESOLVED": UNRESOLVED_DATA,
    "TELEPORT_SOURCE_CLEANUP_FAILED": CLEANUP_FAILED_DATA,
}


def teleport_errors() -> tuple[MethodError, ...]:
    declared = tuple(
        MethodError(
            code,
            data_schema=_DATA.get(code),
            description=_DESCRIPTIONS[code]
            + ("" if code in _DATA else " No data payload."),
        )
        for code in sorted(TELEPORT_ERROR_CODES)
    )
    return (
        MethodError(
            ipc_errors.INVALID_ARGUMENT,
            description="agent or to is not a non-empty string, or dryRun is not a boolean; no data payload.",
        ),
        MethodError(
            ipc_errors.AGENT_NOT_FOUND,
            description="No agent on this node answers to that name; no data payload.",
        ),
        MethodError(ipc_errors.USER_AGENT_IMMUTABLE, description="The named agent is the owner's user agent, which is never administered; no data payload."),
        *declared,
    )
