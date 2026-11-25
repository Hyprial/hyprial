"""Participant-scoped conversation history over the existing daemon authority."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.identity import InvalidAgentNameError
from hyprial.kernel import HttpBinding, IdentityBinding, MethodError, MethodSpec, ipc_errors

from ..address_errors import AGENT_ADDRESS_RETIRED_ERROR

_STRING = {"type": "string", "minLength": 1}
_NULLABLE_STRING = {"type": ["string", "null"]}
_PARAMS = {
    "type": "object",
    "properties": {
        "actor": _STRING,
        "from": _NULLABLE_STRING,
        "sessionRef": {**_STRING, "x-hyprial-http-forbidden": True},
        "onBehalfOf": {**_NULLABLE_STRING, "x-hyprial-http-forbidden": True},
        "conversationId": _STRING,
        "cursor": _NULLABLE_STRING,
        "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 200},
        "mode": {"enum": ["tail", "older", "after", None]},
    },
    "required": ["conversationId"],
    "additionalProperties": False,
}
_ENTRY = {
    "type": "object",
    "properties": {
        "kind": {"const": "message"}, "messageId": _STRING,
        "conversationId": _STRING, "from": _STRING, "to": _STRING,
        "intent": _STRING, "createdAtMs": {"type": "integer"},
        "message": {"type": "string"}, "origin": {"type": "object"},
        "messageClass": {"enum": ["system", "interaction"]},
    },
    "required": ["kind", "messageId", "conversationId", "from", "to", "intent", "createdAtMs", "message", "origin"],
    "additionalProperties": False,
}
_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True}, "conversationId": _STRING,
        "entries": {"type": "array", "items": _ENTRY},
        "mode": {"enum": ["tail", "older", "after"]},
        "nextCursor": _NULLABLE_STRING,
        "headCursor": _NULLABLE_STRING,
        "nextOlderCursor": _NULLABLE_STRING,
    },
    "required": ["ok", "conversationId", "entries", "nextCursor"],
    "additionalProperties": False,
}
def _data(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "additionalProperties": False}

MESSAGE_HISTORY_SPEC = MethodSpec(
    name="message.history", owner="daemon.inbox",
    params_schema=_PARAMS, result_schema=_RESULT,
    errors=(
        MethodError(ipc_errors.INVALID_ARGUMENT, _data(unexpectedFields={"type": "array", "items": _STRING})),
        MethodError(ipc_errors.SENDER_UNRESOLVED, _data(sender=_STRING)),
        AGENT_ADDRESS_RETIRED_ERROR,
        MethodError(ipc_errors.SENDER_NOT_AUTHENTICATED, _data(claimedIdentity=_STRING)),
        MethodError(ipc_errors.AMBIGUOUS_TARGET, _data(target=_STRING, candidates={"type": "array", "items": _STRING})),
        MethodError(InvalidAgentNameError.code),
        MethodError(ipc_errors.STALE_SESSION),
        MethodError(ipc_errors.SESSION_SUPERSEDED),
        MethodError(ipc_errors.INVALID_SESSION_SOURCE),
    ),
    mode="unary", mutates=False, retry="safe",
    audience=("local-operator", "bound-agent"), http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("local-owner-user-selector", "actor"), readiness="inbox-service"),
    confirmation="none",
)


def message_history(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> Any:
    return app._ipc_message_history("message.history", dict(params), _trusted_message_origin=trusted_message_origin)


MESSAGE_HISTORY_BINDING = (MESSAGE_HISTORY_SPEC, message_history)
