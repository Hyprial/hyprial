"""Declare the GUI's read-only send lookup without growing legacy dispatch."""

from collections.abc import Mapping
from typing import Any

from hyprial.identity import InvalidAgentNameError
from hyprial.kernel import HttpBinding, IdentityBinding, IdempotencySpec, MethodError, MethodSpec, ipc_errors

from ..address_errors import AGENT_ADDRESS_RETIRED_ERROR

_STRING = {"type": "string", "minLength": 1}
_PARAMS = {
    "type": "object",
    "properties": {
        name: _STRING for name in (
            "from", "actor", "sessionRef", "target", "text",
            "conversationId", "idempotencyKey",
        )
    },
    "required": ["target", "text", "conversationId", "idempotencyKey"],
    "additionalProperties": False,
}
_PARAMS["properties"]["sessionRef"] = {**_STRING, "x-hyprial-http-forbidden": True}
_RESULT = {
    "type": "object",
    "properties": {
        "status": {"enum": ["accepted", "unknown"]},
        "commandId": _STRING, "conversationId": _STRING, "messageId": _STRING,
        "reason": _STRING,
    },
    "additionalProperties": False,
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "status": {"const": "accepted"},
                "commandId": _STRING,
                "conversationId": _STRING,
                "messageId": _STRING,
            },
            "required": ["status", "commandId", "conversationId", "messageId"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "status": {"const": "unknown"},
                "reason": {"enum": [
                    "not-found", "invalid-record", "request-mismatch", "unavailable",
                ]},
            },
            "required": ["status", "reason"],
            "additionalProperties": False,
        },
    ],
}


def _data(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "additionalProperties": False}


MESSAGE_SEND_LOOKUP_SPEC = MethodSpec(
    name="message.send.lookup",
    owner="daemon.inbox",
    params_schema=_PARAMS,
    result_schema=_RESULT,
    errors=(
        MethodError(ipc_errors.INVALID_ARGUMENT, _data(sender=_STRING)),
        MethodError(ipc_errors.SENDER_UNRESOLVED, _data(sender=_STRING)),
        AGENT_ADDRESS_RETIRED_ERROR,
        MethodError(ipc_errors.SENDER_NOT_AUTHENTICATED, _data(claimedIdentity=_STRING)),
        MethodError(ipc_errors.AMBIGUOUS_TARGET, _data(
            target=_STRING, candidates={"type": "array", "items": _STRING},
        )),
        MethodError(InvalidAgentNameError.code),
        MethodError(ipc_errors.STALE_SESSION),
        MethodError(ipc_errors.SESSION_SUPERSEDED),
        MethodError(ipc_errors.INVALID_SESSION_SOURCE),
    ),
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator", "bound-agent"),
    http_exposed=True,
    # _actor(params) prefers actor over from. The HTTP entry binds the trusted
    # node owner's user identity while IPC retains either legacy alias.
    http_binding=HttpBinding(IdentityBinding("local-owner-user-selector", "actor"), readiness="inbox-service"),
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-send-lookup"),
    mock_echo={"idempotencyKey": "commandId"},
)


def message_send_lookup(
    app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None,
) -> Any:
    # Keep authentication, session fencing and durable sender-copy authority in
    # the existing handler. Registration does not migrate the message family.
    return app._ipc_message_send_lookup(
        "message.send.lookup", dict(params),
        _trusted_message_origin=trusted_message_origin,
    )


MESSAGE_LOOKUP_BINDINGS = ((MESSAGE_SEND_LOOKUP_SPEC, message_send_lookup),)
