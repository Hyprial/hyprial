"""Read-only ``agent`` request declarations and their real handlers.

Decision record (M-01, 2026-10-31)
- ``agent.list``, ``agent.secret.list`` and ``agent.resolve`` are bound by
  exact method name; the ``agent`` family is deliberately *not* in
  ``MIGRATED_FAMILIES`` because every other ``agent.*`` name must keep reaching
  the legacy router unchanged.  This module is therefore the minimal partial
  registration point, not a family migration.
- The read handlers delegate to ``app._handle_agent`` so the existing business
  logic, return shape and authorization order stay the single implementation.
  The typed-error translation copied here is the router's own agent boundary
  (``AgentError``/``DomainCommandError``/``AgentHomeError`` to
  ``DaemonRequestError``); without it a declared read would surface as a generic
  daemon error instead of the legacy code.  Since implBc round 2 the
  translation is shared with the lifecycle bindings (the helper now lives in
  ``entity.call_with_legacy_agent_errors``); this module only applies it.
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
  admitted and only ``true`` filters.  ``includeUserAgent`` (UA1f) follows
  the same rule: only ``true`` adds the user agent's row, so the default
  result stays what it was before the user agent existed.  Unknown keys stay admitted too: the
  legacy handlers ignored them, and closing the object would silently reject
  callers that work today.  The MethodSpec ABI forbids a literal
  ``additionalProperties: true``, so the open allowance is spelled as an
  explicit all-JSON type union -- an honest declaration of the compatibility
  behavior rather than an empty schema.
- The agent row is closed at the top level so a new field becomes a reviewed
  declaration change; nested bags that other producers already extend
  (``capabilities``, ``config.sharedCredentials`` values) stay permissive.
  ``runState``, ``config`` and ``sharedCredentials`` rows are declared from
  their real producers because they are stable, closed shapes.  Since
  implBc round 2 the row shape is built by ``entity.agent_status_row`` so the
  create result reuses the same producer-backed declaration.
- ``http_exposed`` is declaration metadata only.  No HTTP server, binding or
  identity projection is imported here; the H line projects routes from this
  catalog after the shared ABI decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.identity import AgentError, SecretSource
from hyprial.kernel import (
    IdempotencySpec,
    MethodError,
    MethodSpec,
    ipc_errors,
)

from .cell import CELL_BINDINGS
from .entity import agent_status_row, translated_agent_errors
from .home_preparation import BINDINGS as HOME_PREPARATION_BINDINGS
from .harness_args import BINDINGS as HARNESS_ARGS_BINDINGS

JsonObject = dict[str, Any]

PARTIAL_MIGRATED_METHODS = frozenset(
    {
        "agent.ensure-home",
        "agent.get",
        "agent.readiness",
        "agent.runtime-context",
        "agent.list",
        "agent.resolve",
        "agent.secret.list",
        "agent.home-census",
        "agent.harness-args.get",
        "agent.harness-args.validate",
        "agent.harness-args.set",
        "agent.harness-args.clear",
        "agent.cell.get",
        "agent.cell.update",
    }
)

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
        "includeUserAgent": {
            **_ANY_JSON,
            "description": (
                "Only true adds this machine's user agent (the owner's own "
                "agent, no harness) to agents, marked userAgent: true and "
                "userActor: user:<owner>; omitted or any other value hides "
                "it. It is never in agentsWithoutRoutine nor in an "
                "inactiveSince listing."
            ),
        },
    },
    "additionalProperties": _ANY_JSON,
}

_AGENT_SECRET_LIST_PARAMS = {
    "type": "object",
    "properties": {
        "actor": {"type": ["string", "null"], "minLength": 1},
        "expectedAgent": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "uri": {"type": "string", "minLength": 1},
                        "createdAtMs": {"type": "integer", "minimum": 0},
                    },
                    "required": ["uri", "createdAtMs"],
                    "additionalProperties": False,
                },
            ],
        },
    },
    "allOf": [{
        "type": "object",
        "if": {"type": "object", "required": ["expectedAgent"], "properties": {"expectedAgent": {"type": "object"}}},
        "then": {"type": "object", "required": ["actor"], "properties": {"actor": {"type": "string", "minLength": 1}}},
    }],
    "additionalProperties": _ANY_JSON,
}

_AGENT_ROW = agent_status_row(with_activity=True)

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
        MethodError(
            ipc_errors.AGENT_INCARNATION_MISMATCH,
            description="The target agent no longer matches expectedAgent; no data payload.",
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



AGENT_RESOLVE_SPEC = MethodSpec(
    name="agent.resolve", owner="daemon.agent",
    params_schema={"type": "object", "properties": {"name": {"type": "string", "minLength": 1}},
                   "required": ["name"], "additionalProperties": _ANY_JSON},
    result_schema={"type": "object", "properties": {
        "ok": {"const": True}, "input": {"type": "string"},
        "resolved": {"type": "string"}, "known": {"type": "boolean"},
        "reason": {"enum": ["alias", "node", "scheme", "unknown"]},
    }, "required": ["ok", "input", "resolved", "known", "reason"], "additionalProperties": False},
    errors=(
        MethodError(ipc_errors.INVALID_ARGUMENT),
        MethodError(ipc_errors.AMBIGUOUS_TARGET, {"type": "object", "properties": {
            "target": {"type": "string"},
            "candidates": {"type": "array", "items": {"type": "string"}},
        }, "required": ["target", "candidates"], "additionalProperties": False}),
        _agent_error(),
    ),
    mode="unary", mutates=False, retry="safe", audience=("local-operator",),
    http_exposed=False, confirmation="none", ipc_param_validation="handler",
    idempotency=IdempotencySpec(mode="safe", scope="agent-resolution"),
)

def _legacy_agent_call(app: Any, method: str, params: Mapping[str, Any]) -> JsonObject:
    from .entity import call_with_legacy_agent_errors

    return call_with_legacy_agent_errors(
        lambda: app._handle_agent(method, dict(params))
    )


def agent_resolve(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> JsonObject:
    return _legacy_agent_call(app, "agent.resolve", params)

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
    (AGENT_RESOLVE_SPEC, agent_resolve),
    (AGENT_SECRET_LIST_SPEC, agent_secret_list, _secret_list_param_error),
    *HARNESS_ARGS_BINDINGS,
    *CELL_BINDINGS,
    *HOME_PREPARATION_BINDINGS,
)

__all__ = [
    "AGENT_BINDINGS",
    "AGENT_LIST_SPEC",
    "AGENT_SECRET_LIST_SPEC",
    "PARTIAL_MIGRATED_METHODS",
    "agent_list",
    "agent_secret_list",
    "translated_agent_errors",
]
