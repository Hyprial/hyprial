"""The ``session.turn.refresh`` method: declaration plus real handler.

The interactive carrier's current-run refresh entered through the request
catalog, not the frozen legacy router. Its params and result are validated by
the shared ``MethodRegistry`` before and after the handler runs; the handler is
the session-fenced observation path in ``application/actors/sessions/turns.py``
and adds no business authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.daemon.impl.harnesses.carrier.contract import (
    IPC_SESSION_TURN_REFRESH,
)
from hyprial.kernel import IdempotencySpec, MethodError, MethodSpec, ipc_errors

JsonObject = dict[str, Any]

_CAPABILITIES = {
    "type": "object",
    "properties": {
        "wholeSessionTurns": {"type": "boolean"},
        "preparation": {"type": "boolean"},
        "permissionWait": {"type": "boolean"},
        "producerActivity": {"type": "boolean"},
    },
    "required": [
        "wholeSessionTurns",
        "preparation",
        "permissionWait",
        "producerActivity",
    ],
    "additionalProperties": False,
}

_OPEN_RUN = {
    "type": "object",
    "properties": {
        "reportId": {"type": "string", "minLength": 1, "maxLength": 128},
        "phase": {"type": "string", "enum": ["prepared", "executing"]},
        "elapsedMs": {"type": "integer", "minimum": 0, "maximum": 604800000},
    },
    "required": ["reportId", "phase"],
    "additionalProperties": False,
}

_PARAMS = {
    "type": "object",
    "properties": {
        "actor": {"type": "string", "minLength": 1},
        "sessionRef": {"type": "string", "minLength": 1},
        "schemaVersion": {"type": "integer", "enum": [1]},
        "producerEpoch": {"type": "string", "minLength": 1, "maxLength": 128},
        "daemonEpoch": {"type": "string", "minLength": 1, "maxLength": 128},
        "seq": {"type": "integer", "minimum": 0},
        "openRuns": {
            "type": "array",
            "maxItems": 64,
            "items": _OPEN_RUN,
        },
        "capabilities": _CAPABILITIES,
        "channelLeaseToken": {"type": "string", "minLength": 32, "maxLength": 256},
    },
    "required": [
        "actor",
        "sessionRef",
        "schemaVersion",
        "producerEpoch",
        "seq",
        "openRuns",
        "capabilities",
    ],
    "additionalProperties": False,
}

_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "schemaVersion": {"type": "integer", "enum": [1]},
        "refreshedAtMs": {"type": "integer", "minimum": 0},
        "applied": {"type": "boolean"},
        "resyncCleared": {"type": "boolean"},
        "observationGapCleared": {"type": "boolean"},
        "rejectionReason": {
            "enum": [None, "retired_producer", "producer_history_exhausted",
                     "stale_sequence", "mount_unavailable", "stale_daemon_epoch"],
        },
    },
    "required": [
        "ok",
        "schemaVersion",
        "refreshedAtMs",
        "applied",
        "resyncCleared",
        "observationGapCleared",
        "rejectionReason",
    ],
    "additionalProperties": False,
}

SESSION_TURN_REFRESH_SPEC = MethodSpec(
    name=IPC_SESSION_TURN_REFRESH,
    owner="daemon.sessions",
    params_schema=_PARAMS,
    result_schema=_RESULT,
    errors=(
        MethodError(
            ipc_errors.INVALID_ARGUMENT,
            description="payload, session or ledger fence rejected the report",
        ),
        MethodError(
            ipc_errors.STALE_SESSION,
            description="no current interactive session owns this actor",
        ),
        MethodError(
            ipc_errors.SESSION_SUPERSEDED,
            description="a newer session owns the actor, or it was retired",
        ),
        MethodError(
            ipc_errors.INVALID_CHANNEL_LEASE,
            description="the lease does not match the registered session",
        ),
        MethodError(
            ipc_errors.CHANNEL_LIVENESS_UNAVAILABLE,
            description="a lease-backed session has no operational lease",
        ),
    ),
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator", "bound-agent"),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="current-run-observation"),
)


def session_turn_refresh(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return app._ipc_session_turn_refresh(dict(params))


SESSION_BINDINGS = ((SESSION_TURN_REFRESH_SPEC, session_turn_refresh),)

__all__ = [
    "SESSION_BINDINGS",
    "SESSION_TURN_REFRESH_SPEC",
    "session_turn_refresh",
]
