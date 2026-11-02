"""The two migrated read-only ``agent`` methods: declarations and real handlers.

Decision record (M-01, 2026-10-31)
- Scope is exactly ``agent.list`` and ``agent.secret.list``.  They are bound by
  exact method name; the ``agent`` family is deliberately *not* in
  ``MIGRATED_FAMILIES`` because every other ``agent.*`` name must keep reaching
  the legacy router unchanged.  This module is therefore the minimal partial
  registration point, not a family migration.
- Both handlers delegate to ``app._handle_agent`` so the existing business
  logic, return shape and authorization order stay the single implementation.
  The typed-error translation copied here is the router's own agent boundary
  (``AgentError``/``DomainCommandError``/``AgentHomeError`` to
  ``DaemonRequestError``); without it a declared read would surface as a generic
  daemon error instead of the legacy code.
- ``agent secret ls`` is the grant inventory of one selected agent (or of every
  registered agent when ``actor`` is omitted), never the local secret-store list
  behind ``agent secret list --agent``.  The target agent is a business
  parameter; no caller identity is accepted or inferred here.
- Params schemas describe the accepted JSON shape *as the existing handler
  accepts it*, not a new stricter one.  ``inactiveSince`` keeps its duration
  grammar inside ``parse_duration`` (one authority for numbers and
  ``<n><ms|s|m|h|d>`` strings); the schema only declares that it may be a
  number, a string or null, so the handler keeps raising the legacy
  ``INVALID_ARGUMENT`` text for booleans, containers and non-positive values.
  ``excludeWf`` is read with ``is True`` by the handler, so any JSON value is
  admitted and only ``true`` filters.  Unknown keys stay admitted too: the
  legacy handlers ignored them, and closing the object would silently reject
  callers that work today.  The MethodSpec ABI forbids a literal
  ``additionalProperties: true``, so the open allowance is spelled as an
  explicit all-JSON type union -- an honest declaration of the compatibility
  behavior rather than an empty schema.
- The agent row is closed at the top level so a new field becomes a reviewed
  declaration change; nested bags that other producers already extend
  (``capabilities``, ``config.sharedCredentials`` values) stay permissive.
  ``runState``, ``config`` and ``sharedCredentials`` rows are declared from
  their real producers because they are stable, closed shapes.
- ``http_exposed`` is declaration metadata only.  No HTTP server, binding or
  identity projection is imported here; the H line projects routes from this
  catalog after the shared ABI decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, get_args

from ...cells.observability.model import (
    BaseRunState, ObservationHealth, RunCertainty, RunScope, RunState,
)

from hyprial.identity import AgentError, AgentHomeError, HOSTED_BY_VALUES, SecretSource
from hyprial.kernel import (
    DaemonRequestError,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    ipc_errors,
)

JsonObject = dict[str, Any]

PARTIAL_MIGRATED_METHODS = frozenset({"agent.list", "agent.secret.list"})

_AUDIENCE = ("local-operator", "bound-agent")


def _agent_error() -> MethodError:
    return MethodError(
        AgentError.code,
        description="The local agent registry could not be read; no data payload.",
    )


_ANY_JSON = {"type": ["string", "number", "boolean", "null", "array", "object"]}

_AGENT_LIST_PARAMS = {
    "type": "object",
    "properties": {
        "inactiveSince": {"type": ["number", "string", "null"]},
        "excludeWf": _ANY_JSON,
    },
    "additionalProperties": _ANY_JSON,
}

_AGENT_SECRET_LIST_PARAMS = {
    "type": "object",
    "properties": {
        "actor": {"type": ["string", "null"], "minLength": 1},
    },
    "additionalProperties": _ANY_JSON,
}

_CAPABILITIES = {"type": "object"}

_CONFIG_SOURCE = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "required": {"type": "boolean"},
    },
    "required": ["path", "required"],
    "additionalProperties": False,
}

_CONFIG = {
    "oneOf": [
        {"type": "null"},
        {
            "type": "object",
            "properties": {
                "sources": {"type": "array", "items": _CONFIG_SOURCE},
                "discovery": {"const": "explicit-only"},
                "sharedCredentials": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["sources", "discovery"],
            "additionalProperties": False,
        },
    ]
}

_HARNESS_ARGS = {
    "type": "object",
    "additionalProperties": {"type": "array", "items": {"type": "string"}},
}

_ACTIVITY_HINTS = {
    "type": "object",
    "properties": {
        "createdAtMs": {"type": "integer"},
        "hasLastSessionId": {"type": "boolean"},
        "agentHomeMtimeMs": {"type": ["integer", "null"]},
    },
    "required": ["createdAtMs", "hasLastSessionId", "agentHomeMtimeMs"],
    "additionalProperties": False,
}

_RUN_COVERAGE = {
    "type": "object",
    "properties": {
        "turnBoundaries": {"type": "boolean"},
        "permissionWait": {"type": "boolean"},
        "producerActivity": {"type": "boolean"},
        "processExit": {"type": "boolean"},
    },
    "required": [
        "turnBoundaries",
        "permissionWait",
        "producerActivity",
        "processExit",
    ],
    "additionalProperties": False,
}

_RUN_THRESHOLDS = {
    "type": "object",
    "properties": {
        "turnLongAfterMs": {"type": ["integer", "null"]},
        "permissionLongAfterMs": {"type": ["integer", "null"]},
        "quietAfterMs": {"type": ["integer", "null"]},
    },
    "required": ["turnLongAfterMs", "permissionLongAfterMs", "quietAfterMs"],
    "additionalProperties": False,
}

_RUN_LAST_KNOWN = {
    "type": "object",
    "properties": {
        "state": {"type": "string"},
        "baseState": {"type": "string"},
        "sinceMs": {"type": ["integer", "null"]},
        "observedAtMs": {"type": "integer"},
        "runtimeIncarnation": {"type": "string"},
    },
    "required": [
        "state",
        "baseState",
        "sinceMs",
        "observedAtMs",
        "runtimeIncarnation",
    ],
    "additionalProperties": False,
}

_RUN_STATE = {
    "type": "object",
    "properties": {
        "state": {"enum": list(get_args(RunState))},
        "baseState": {"enum": list(get_args(BaseRunState))},
        "reason": {"type": "string", "minLength": 1},
        "certainty": {"enum": list(get_args(RunCertainty))},
        "scope": {"enum": list(get_args(RunScope))},
        "observedAtMs": {"type": "integer"},
        "sinceMs": {"type": ["integer", "null"]},
        "turnAgeMs": {"type": ["integer", "null"]},
        "lastActivityAtMs": {"type": ["integer", "null"]},
        "coverage": _RUN_COVERAGE,
        "observationHealth": {"enum": list(get_args(ObservationHealth))},
        "suspicions": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "policyRevision": {"type": "integer"},
        "providerBlocked": {"type": "boolean"},
        "thresholds": _RUN_THRESHOLDS,
        "lastKnown": {"oneOf": [{"type": "null"}, _RUN_LAST_KNOWN]},
        "runtimeIncarnation": {"type": "string", "minLength": 1},
        "epoch": {"type": "string", "minLength": 1},
        "revision": {"type": "integer"},
    },
    "required": [
        "state",
        "baseState",
        "reason",
        "certainty",
        "scope",
        "observedAtMs",
        "sinceMs",
        "turnAgeMs",
        "lastActivityAtMs",
        "coverage",
        "observationHealth",
        "suspicions",
        "policyRevision",
        "providerBlocked",
        "thresholds",
        "lastKnown",
        "runtimeIncarnation",
        "epoch",
        "revision",
    ],
    "additionalProperties": False,
}

_SHARED_CREDENTIAL_ROW = {
    "type": "object",
    "properties": {
        "harness": {"type": "string", "minLength": 1},
        "authMode": {"const": "native-shared-link"},
        "nativePath": {"type": "string", "minLength": 1},
        "targetPath": {"type": "string", "minLength": 1},
        "status": {"enum": ["error", "warning", "ready"]},
        "code": {"type": "string", "minLength": 1},
        "error": {"type": "string", "minLength": 1},
        "warning": {"type": "string", "minLength": 1},
    },
    "required": ["harness", "authMode", "targetPath", "status"],
    "additionalProperties": False,
}

_RECOVERY_ERROR = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "minLength": 1},
        "errorType": {"type": "string", "minLength": 1},
        "error": {"type": "string"},
        "source": {"type": "string", "minLength": 1},
        "harness": {"type": ["string", "null"]},
        "runtime": {"type": ["string", "null"]},
        "sessionId": {"type": ["string", "null"]},
    },
    "required": [
        "code",
        "errorType",
        "error",
        "source",
        "harness",
        "runtime",
        "sessionId",
    ],
    "additionalProperties": False,
}

_RECOVERY_CLEANUP = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "minLength": 1},
        "harness": {"type": ["string", "null"]},
        "runtime": {"type": ["string", "null"]},
        "sessionId": {"type": ["string", "null"]},
        "processPid": {"type": ["integer", "null"]},
        "liveness": {"type": ["string", "null"]},
    },
    "required": [
        "source",
        "harness",
        "runtime",
        "sessionId",
        "processPid",
        "liveness",
    ],
    "additionalProperties": False,
}

_AGENT_ROW = {
    "type": "object",
    "properties": {
        "uri": {"type": "string", "minLength": 1},
        "actor": {"type": "string", "minLength": 1},
        "owner": {"type": "string", "minLength": 1},
        "machine": {"type": "string", "minLength": 1},
        "cwd": {"type": ["string", "null"]},
        "config": _CONFIG,
        "provider": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "capabilities": _CAPABILITIES,
        "harnessArgs": _HARNESS_ARGS,
        "preferredHarness": {"type": ["string", "null"]},
        "lastHarness": {"type": ["string", "null"]},
        "lastSessionId": {"type": ["string", "null"]},
        "lastActiveAtMs": {"type": ["integer", "null"]},
        "pinnedAdapters": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "createdAtMs": {"type": "integer"},
        "hosted": {"type": "boolean"},
        "hostedBy": {
            "type": ["string", "null"],
            "enum": [None, *HOSTED_BY_VALUES],
        },
        "activityHints": _ACTIVITY_HINTS,
        "status": {"type": "string", "minLength": 1},
        "harness": {"type": ["string", "null"]},
        "runtime": {"type": ["string", "null"]},
        "sessionId": {"type": ["string", "null"]},
        "heartbeatAgeSeconds": {"type": ["number", "null"]},
        "runState": _RUN_STATE,
        "workerState": {"enum": ["blocked", "dormant"]},
        "restoreStatus": {"type": "string", "minLength": 1},
        "blockReason": {"type": "string", "minLength": 1},
        "blockedAtMs": {"type": "integer"},
        "blockedAgeMs": {"type": "integer"},
        "idleAgeMs": {"type": ["integer", "null"]},
        "restoreThresholdMs": {"type": "integer"},
        "restoreOverride": {"type": "string", "minLength": 1},
        "activityUnknown": {"type": "boolean"},
        "sharedCredentials": {
            "type": "array",
            "items": _SHARED_CREDENTIAL_ROW,
        },
        "recoveryStatus": {"enum": ["failed", "cleaned"]},
        "recoveryError": _RECOVERY_ERROR,
        "recoveryCleanup": _RECOVERY_CLEANUP,
    },
    "required": [
        "uri",
        "actor",
        "owner",
        "machine",
        "cwd",
        "config",
        "provider",
        "model",
        "capabilities",
        "harnessArgs",
        "preferredHarness",
        "lastHarness",
        "lastSessionId",
        "lastActiveAtMs",
        "pinnedAdapters",
        "createdAtMs",
        "hosted",
        "hostedBy",
        "activityHints",
        "status",
        "harness",
        "runtime",
        "sessionId",
        "heartbeatAgeSeconds",
    ],
    "additionalProperties": False,
}

_AGENT_LIST_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "agents": {"type": "array", "items": _AGENT_ROW},
        "agentsWithoutRoutine": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
    },
    "required": ["ok", "agents", "agentsWithoutRoutine"],
    "additionalProperties": False,
}

_SECRET_SOURCE = {"enum": [source.value for source in SecretSource]}

_SECRET_GRANT_ROW = {
    "type": "object",
    "properties": {
        "actor": {"type": "string", "minLength": 1},
        "grantId": {"type": "string", "minLength": 1},
        "source": _SECRET_SOURCE,
        "entryId": {"type": "string", "minLength": 1},
        "fieldName": {"type": ["string", "null"]},
        "environmentNames": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "revision": {"type": "integer", "minimum": 1},
        "ownerActor": {"type": ["string", "null"]},
        "ownerEntityToken": {"type": ["string", "null"]},
    },
    "required": [
        "actor",
        "grantId",
        "source",
        "entryId",
        "fieldName",
        "environmentNames",
        "revision",
        "ownerActor",
        "ownerEntityToken",
    ],
    "additionalProperties": False,
}

_AGENT_SECRET_LIST_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "grants": {"type": "array", "items": _SECRET_GRANT_ROW},
    },
    "required": ["ok", "grants"],
    "additionalProperties": False,
}

AGENT_LIST_SPEC = MethodSpec(
    name="agent.list",
    owner="daemon.agent",
    params_schema=_AGENT_LIST_PARAMS,
    result_schema=_AGENT_LIST_RESULT,
    errors=(
        MethodError(
            ipc_errors.INVALID_ARGUMENT,
            description="inactiveSince is not a positive number or duration string; no data payload.",
        ),
        _agent_error(),
    ),
    mode="unary",
    mutates=False,
    retry="safe",
    audience=_AUDIENCE,
    http_exposed=True,
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)

AGENT_SECRET_LIST_SPEC = MethodSpec(
    name="agent.secret.list",
    owner="daemon.agent.secret",
    params_schema=_AGENT_SECRET_LIST_PARAMS,
    result_schema=_AGENT_SECRET_LIST_RESULT,
    errors=(
        MethodError(
            ipc_errors.INVALID_ARGUMENT,
            description="actor must be null or a non-empty agent name; no data payload.",
        ),
        MethodError(
            ipc_errors.AGENT_NOT_FOUND,
            description="No agent with that name is registered on this machine; no data payload.",
        ),
        _agent_error(),
    ),
    mode="unary",
    mutates=False,
    retry="safe",
    audience=_AUDIENCE,
    http_exposed=True,
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)


def _legacy_agent_call(app: Any, method: str, params: Mapping[str, Any]) -> JsonObject:
    from hyprial.daemon.impl.composition import DomainCommandError

    try:
        return app._handle_agent(method, dict(params))
    except (AgentError, DomainCommandError) as error:
        raise DaemonRequestError(
            error.code, str(error), data=getattr(error, "data", None)
        ) from error
    except AgentHomeError as error:
        raise DaemonRequestError(AgentError.code, str(error)) from error


def agent_list(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return _legacy_agent_call(app, "agent.list", params)


def agent_secret_list(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return _legacy_agent_call(app, "agent.secret.list", params)


def _path(error: Any) -> list[str]:
    return [str(part) for part in getattr(error, "absolute_path", ())]


def _list_param_error(error: Any, params: Mapping[str, Any]) -> str:
    path = _path(error)
    field = path[0] if path else ""
    if field == "inactiveSince":
        instance = getattr(error, "instance", None)
        if isinstance(instance, bool):
            return "inactiveSince must be a duration, not a boolean"
        return f"inactiveSince must be a duration, got {type(instance).__name__}"
    return _generic_param_error(AGENT_LIST_SPEC.name, error)


def _secret_list_param_error(error: Any, params: Mapping[str, Any]) -> str:
    path = _path(error)
    if path[:1] == ["actor"]:
        return "actor must be a non-empty string"
    return _generic_param_error(AGENT_SECRET_LIST_SPEC.name, error)


def _generic_param_error(method: str, error: Any) -> str:
    path = "/".join(_path(error)) or "<root>"
    return f"{method} params do not match the declared schema at {path}"


AGENT_BINDINGS = (
    (AGENT_LIST_SPEC, agent_list, _list_param_error),
    (AGENT_SECRET_LIST_SPEC, agent_secret_list, _secret_list_param_error),
)

__all__ = [
    "AGENT_BINDINGS",
    "AGENT_LIST_SPEC",
    "AGENT_SECRET_LIST_SPEC",
    "PARTIAL_MIGRATED_METHODS",
    "agent_list",
    "agent_secret_list",
]
