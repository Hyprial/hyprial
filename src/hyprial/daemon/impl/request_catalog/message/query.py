"""Non-consuming inbox/outbox query using the established daemon reader."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.identity import InvalidAgentNameError
from hyprial.kernel import HttpBinding, IdentityBinding, MethodError, MethodSpec, ipc_errors

_STRING = {"type": "string", "minLength": 1}
_VALUE = {"type": ["string", "number", "boolean", "null", "array", "object"]}
_PARAMS = {
    "type": "object",
    "properties": {
        "actor": _STRING, "from": {"type": ["string", "null"]},
        "sessionRef": {**_VALUE, "x-hyprial-http-forbidden": True}, "onBehalfOf": {**_VALUE, "x-hyprial-http-forbidden": True},
        "view": {"enum": ["inbox", "outbox"]},
    },
    "required": ["view"],
    # The legacy query ignores unrelated fields. Keep that IPC boundary while
    # the HTTP selector remains strictly bound by the request entry.
    "additionalProperties": _VALUE,
}
_ENTRY = {
    "type": "object",
    "properties": {
        "kind": _STRING, "messageId": _STRING, "conversationId": _STRING,
        "from": _STRING, "to": _STRING, "intent": _STRING,
        "createdAtMs": {"type": "integer"}, "message": {"type": "string"},
        "origin": {"type": "object"},
        "sender": _STRING, "recipient": _STRING,
        "attempts": {"type": "integer"}, "failedAttempts": {"type": "integer"},
        "nextAttemptMs": {"type": ["integer", "null"]},
        "expiresAtMs": {"type": ["integer", "null"]},
        "undeliverableScheme": {"type": "boolean"},
    },
    "required": ["messageId", "conversationId"],
    "additionalProperties": False,
}
_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True}, "actor": _STRING,
        "view": {"enum": ["inbox", "outbox"]},
        "entries": {"type": "array", "items": _ENTRY},
    },
    "required": ["ok", "actor", "view", "entries"],
    "additionalProperties": False,
}


def _data(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "additionalProperties": False}


MESSAGE_QUERY_SPEC = MethodSpec(
    name="message.query", owner="daemon.inbox",
    params_schema=_PARAMS, result_schema=_RESULT,
    errors=(
        MethodError(ipc_errors.INVALID_ARGUMENT),
        MethodError(ipc_errors.SENDER_UNRESOLVED, _data(sender=_STRING)),
        MethodError(ipc_errors.AMBIGUOUS_TARGET, _data(target=_STRING, candidates={"type": "array", "items": _STRING})),
        MethodError(InvalidAgentNameError.code),
    ),
    mode="unary", mutates=False, retry="safe",
    audience=("local-operator",), http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("local-owner-user-selector", "actor"), readiness="inbox-service"),
    confirmation="none",
)


def message_query(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> Any:
    return app._ipc_message_query(dict(params))


MESSAGE_QUERY_BINDING = (MESSAGE_QUERY_SPEC, message_query)

_OUTBOX_STRING = {"type": "string"}
_OUTBOX_INTEGER = {"type": "integer"}
_OUTBOX_ENTRY = {
    "type": "object",
    "properties": {
        "messageId": _OUTBOX_STRING, "conversationId": _OUTBOX_STRING,
        "sender": _OUTBOX_STRING, "recipient": _OUTBOX_STRING, "intent": _OUTBOX_STRING,
        "createdAtMs": _OUTBOX_INTEGER, "attempts": _OUTBOX_INTEGER,
        "failedAttempts": _OUTBOX_INTEGER, "nextAttemptMs": _OUTBOX_INTEGER,
        "expiresAtMs": _OUTBOX_INTEGER, "undeliverableScheme": {"type": "boolean"},
    },
    "required": ["messageId", "conversationId", "sender", "recipient", "intent",
                 "createdAtMs", "attempts", "failedAttempts", "nextAttemptMs",
                 "expiresAtMs", "undeliverableScheme"],
    "additionalProperties": False,
}
OUTBOX_LIST_SPEC = MethodSpec(
    name="outbox.list", owner="daemon.inbox",
    params_schema={"type": "object", "properties": {}, "additionalProperties": _VALUE},
    result_schema={"type": "object", "properties": {"ok": {"const": True},
                   "entries": {"type": "array", "items": _OUTBOX_ENTRY}},
                   "required": ["ok", "entries"], "additionalProperties": False},
    errors=tuple(MethodError(code) for code in (ipc_errors.INVALID_ARGUMENT,
                                                ipc_errors.DAEMON_RESTORING,
                                                ipc_errors.DAEMON_ERROR)),
    mode="unary", mutates=False, retry="safe", audience=("local-operator", "bound-agent"),
    http_exposed=False, confirmation="none",
)


def outbox_list(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> Any:
    return app._ipc_outbox_list()


OUTBOX_LIST_BINDING = (OUTBOX_LIST_SPEC, outbox_list)
