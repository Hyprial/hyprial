"""MethodSpec declarations for ``agent.cell.get`` and ``agent.cell.update`` (I6-3)."""

from __future__ import annotations

from hyprial.identity import AgentError
from hyprial.kernel import (
    HttpBinding,
    IdentityBinding,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    ipc_errors,
)

AGENT_CELL_GET = "agent.cell.get"
AGENT_CELL_UPDATE = "agent.cell.update"

_STRING = {"type": "string", "minLength": 1}
_ANY_JSON = {"type": ["string", "number", "boolean", "null", "array", "object"]}
_CELL_STATE_ENUM = ["empty", "active", "degraded", "retiring", "retired", "unknown"]
_ACTOR_STATE_ENUM = ["empty", "active", "degraded", "retiring", "retired"]
_MEMBER_PHASE_ENUM = [
    "missing",
    "starting",
    "ready",
    "in-turn",
    "stopping",
    "stopped",
    "failed",
]

_CELL_MEMBER_ITEM = {
    "type": "object",
    "properties": {
        "ref": _STRING,
        "role": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,63}$"},
    },
    "required": ["ref", "role"],
    "additionalProperties": False,
}

_PARAMS_MAP = {
    "type": "object",
    "propertyNames": _STRING,
    "additionalProperties": _ANY_JSON,
}

_AGENT_PARAMS_MAP = {
    "type": "object",
    "additionalProperties": _ANY_JSON,
}

_CELL_SNIPPET = {
    "type": "object",
    "properties": {
        "cellId": _STRING,
        "revision": {"type": "integer", "minimum": 1},
        "members": {"type": "array", "items": _CELL_MEMBER_ITEM},
        "params": _PARAMS_MAP,
    },
    "required": ["cellId", "revision", "members", "params"],
    "additionalProperties": False,
}

_SNAPSHOT_MEMBER = {
    "type": "object",
    "properties": {
        "name": _STRING,
        "role": _STRING,
        "kind": {"type": ["string", "null"], "minLength": 1},
        "phase": {"enum": _MEMBER_PHASE_ENUM},
        "running": {"type": "boolean"},
        "workId": {"type": ["string", "null"], "minLength": 1},
        "lastError": {"type": ["string", "null"]},
        "policyRevision": {"type": ["integer", "null"], "minimum": 0},
        "policyDigest": {"type": ["string", "null"]},
    },
    "required": [
        "name",
        "role",
        "kind",
        "phase",
        "running",
        "workId",
        "lastError",
        "policyRevision",
        "policyDigest",
    ],
    "additionalProperties": False,
}

_ACTIVE_WORK = {
    "oneOf": [
        {"type": "null"},
        {
            "type": "object",
            "properties": {
                "workId": _STRING,
                "member": _STRING,
                "hopIndex": {"type": "integer", "minimum": 0},
                "startedAtMs": {"type": "integer", "minimum": 0},
            },
            "required": ["workId", "member", "hopIndex", "startedAtMs"],
            "additionalProperties": False,
        },
    ]
}

_SNAPSHOT_CONNECTION = {
    "type": "object",
    "properties": {
        "from": _STRING,
        "on": {"type": "array", "items": _STRING},
        "to": _STRING,
        "route": {"type": ["string", "null"], "minLength": 1},
    },
    "required": ["from", "on", "to", "route"],
    "additionalProperties": False,
}

_CELL_ACTOR_SNAPSHOT = {
    "oneOf": [
        {"type": "null"},
        {
            "type": "object",
            "properties": {
                "cellId": _STRING,
                "epoch": _STRING,
                "sequence": {"type": "integer", "minimum": 0},
                "revision": {"type": "integer", "minimum": 0},
                "effectiveRevision": {"type": "integer", "minimum": 0},
                "observedDocumentDigest": {"type": "string"},
                "effectiveDocumentDigest": {"type": "string"},
                "entryMember": {"type": ["string", "null"], "minLength": 1},
                "state": {"enum": _ACTOR_STATE_ENUM},
                "reason": {"type": ["string", "null"]},
                "sourceStale": {"type": "boolean"},
                "updatedAtMs": {"type": "integer", "minimum": 0},
                "members": {"type": "array", "items": _SNAPSHOT_MEMBER},
                "activeWork": _ACTIVE_WORK,
                "queuedWork": {"type": "integer", "minimum": 0},
                "completedWork": {"type": "integer", "minimum": 0},
                "lastTerminal": {"type": ["string", "null"]},
                "connections": {"type": "array", "items": _SNAPSHOT_CONNECTION},
                "failOpen": {"type": ["string", "null"], "minLength": 1},
            },
            "required": [
                "cellId",
                "epoch",
                "sequence",
                "revision",
                "effectiveRevision",
                "observedDocumentDigest",
                "effectiveDocumentDigest",
                "entryMember",
                "state",
                "reason",
                "sourceStale",
                "updatedAtMs",
                "members",
                "activeWork",
                "queuedWork",
                "completedWork",
                "lastTerminal",
                "connections",
                "failOpen",
            ],
            "additionalProperties": False,
        },
    ]
}

_GET_PARAMS = {
    "type": "object",
    "properties": {"name": _STRING},
    "required": ["name"],
    "additionalProperties": False,
}

_GET_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "cell": _CELL_SNIPPET,
        "snapshot": _CELL_ACTOR_SNAPSHOT,
    },
    "required": ["ok", "cell", "snapshot"],
    "additionalProperties": False,
}

_UPDATE_PARAMS = {
    "type": "object",
    "properties": {
        "name": _STRING,
        "expectedRevision": {"type": "integer", "minimum": 1},
        "cellName": {"type": "string"},
        "members": {"type": "array", "items": _CELL_MEMBER_ITEM},
        "params": _AGENT_PARAMS_MAP,
    },
    "required": ["name", "expectedRevision"],
    "additionalProperties": False,
}

_UPDATE_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "cellId": _STRING,
        "revision": {"type": "integer", "minimum": 1},
        "appliedRevision": {"type": "integer", "minimum": 0},
        "state": {"enum": _CELL_STATE_ENUM},
    },
    "required": ["ok", "cellId", "revision", "appliedRevision", "state"],
    "additionalProperties": False,
}

_CAS_CONFLICT_DATA = {
    "type": "object",
    "properties": {
        "expected": {"type": "integer", "minimum": 1},
        "current": {"type": "integer", "minimum": 1},
    },
    "required": ["expected", "current"],
    "additionalProperties": False,
}

_AGENT_ERROR = MethodError(
    AgentError.code,
    description="The local agent registry could not be read; no data payload.",
)

AGENT_CELL_GET_SPEC = MethodSpec(
    name=AGENT_CELL_GET,
    owner="daemon.agent",
    params_schema=_GET_PARAMS,
    result_schema=_GET_RESULT,
    errors=(
        MethodError(
            ipc_errors.INVALID_ARGUMENT,
            description="name is missing or malformed; no data payload.",
        ),
        MethodError(
            ipc_errors.AGENT_NOT_FOUND,
            description="No agent with that name is registered on this machine.",
        ),
        MethodError(
            ipc_errors.USER_AGENT_IMMUTABLE,
            description=(
                "The named agent is the owner's user agent and the caller is not the "
                "host local operator, who alone reads its cell."
            ),
        ),
        _AGENT_ERROR,
        MethodError(
            ipc_errors.DAEMON_ERROR,
            description="Cell runtime or cell document is unavailable.",
        ),
    ),
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator", "bound-agent"),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="agent-cell-read"),
)

AGENT_CELL_UPDATE_SPEC = MethodSpec(
    name=AGENT_CELL_UPDATE,
    owner="daemon.agent",
    params_schema=_UPDATE_PARAMS,
    result_schema=_UPDATE_RESULT,
    errors=(
        MethodError(
            ipc_errors.INVALID_ARGUMENT,
            data_schema=_CAS_CONFLICT_DATA,
            description=(
                "Parameters or cell structure are invalid, immutable fields were supplied, "
                "the update changes the owner's user agent cell outside its adapter members, "
                "their adapter and user connections, "
                "or expectedRevision conflicts with current revision (data carries expected/current)."
            ),
        ),
        MethodError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            description="Only the host local operator may update an agent cell.",
        ),
        MethodError(
            ipc_errors.AGENT_NOT_FOUND,
            description="No agent with that name is registered on this machine.",
        ),
        MethodError(
            ipc_errors.USER_AGENT_IMMUTABLE,
            description=(
                "agent.template of the owner's user agent cell was changed or removed; "
                "only its adapter members, their adapter and user connections may change."
            ),
        ),
        MethodError(
            ipc_errors.ORG_SOURCE_UNREACHABLE,
            description="OrgFS is disconnected; cell editing failed closed.",
        ),
        _AGENT_ERROR,
        MethodError(
            ipc_errors.DAEMON_ERROR,
            description="Cell runtime or cell document is unavailable.",
        ),
    ),
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="agent-cell-update"),
)

CELL_SPECS = (AGENT_CELL_GET_SPEC, AGENT_CELL_UPDATE_SPEC)

__all__ = [
    "AGENT_CELL_GET",
    "AGENT_CELL_GET_SPEC",
    "AGENT_CELL_UPDATE",
    "AGENT_CELL_UPDATE_SPEC",
    "CELL_SPECS",
]
