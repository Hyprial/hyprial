"""The declared, read-only contract for the existing ``message.status`` query.

Semantic notes (I5-4):
- ``state="refused"`` with ``reason="CELL_WORK_REFUSED"`` is also projected for
  explicit cell work refusal settlements recorded via ``harness_failure_settlements``
  without modifying the ``delivery_status`` ledger or changing the IPC schema.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any

from hyprial.kernel import DaemonRequestError, IdempotencySpec, MethodError, MethodSpec, ipc_errors

from ..address_errors import AGENT_ADDRESS_RETIRED_ERROR

JsonObject = dict[str, Any]

_IDENTITY = {"type": "string", "minLength": 1}

MESSAGE_STATUS_PARAMS = {
    "type": "object",
    "properties": {
        "actor": _IDENTITY,
        "from": _IDENTITY,
        "messageId": {"type": ["string", "null"], "minLength": 1},
        "sessionRef": _IDENTITY,
        "onBehalfOf": _IDENTITY,
        "timeoutSeconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": 10,
        },
    },
    "anyOf": [
        {"type": "object", "required": ["actor"]},
        {"type": "object", "required": ["from"]},
    ],
    "additionalProperties": False,
}

_STATUS_RECORD = {
    "type": "object",
    "properties": {
        "messageId": _IDENTITY,
        "sender": _IDENTITY,
        "recipient": _IDENTITY,
        "state": {"enum": ["fetched", "expired", "refused"]},
        "holder": _IDENTITY,
        "reason": _IDENTITY,
        "recordedAtMs": {"type": "integer", "minimum": 0},
        "conversationId": {"type": "string"},
        "idempotencyKey": {"type": ["string", "null"]},
    },
    "required": [
        "messageId",
        "sender",
        "recipient",
        "state",
        "holder",
        "reason",
        "recordedAtMs",
        "conversationId",
        "idempotencyKey",
    ],
    "additionalProperties": False,
}

_DUPLICATE = {
    "type": "object",
    "properties": {
        "holder": _IDENTITY,
        "replies": {"type": "integer", "minimum": 0},
        "records": {"type": "integer", "minimum": 0},
    },
    "required": ["holder", "replies", "records"],
    "additionalProperties": False,
}

_DIAGNOSTICS = {
    "type": "object",
    "properties": {
        "localRecords": {"type": "integer", "minimum": 0},
        "meshReplies": {"type": "integer", "minimum": 0},
        "meshRecords": {"type": "integer", "minimum": 0},
        "meshHolders": {"type": "array", "items": _IDENTITY},
        "meshResponders": {"type": "array", "items": _IDENTITY},
        "undecodableReplies": {"type": "integer", "minimum": 0},
        "undecodableHolders": {"type": "array", "items": _IDENTITY},
        "replyErrors": {"type": "integer", "minimum": 0},
        "meshError": {"type": ["string", "null"]},
        "holders": {"type": "array", "items": _IDENTITY},
        "missingRecipientHolders": {"type": "array", "items": _IDENTITY},
        "conflictingMessageIds": {"type": "array", "items": _IDENTITY},
        "duplicateHolderRecords": {"type": "array", "items": _DUPLICATE},
        "noKnownLoss": {"type": "boolean"},
    },
    "required": [
        "localRecords",
        "meshReplies",
        "meshRecords",
        "meshHolders",
        "meshResponders",
        "undecodableReplies",
        "undecodableHolders",
        "replyErrors",
        "meshError",
        "holders",
        "missingRecipientHolders",
        "conflictingMessageIds",
        "duplicateHolderRecords",
        "noKnownLoss",
    ],
    "additionalProperties": False,
}

MESSAGE_STATUS_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "from": _IDENTITY,
        "messageId": _IDENTITY,
        "state": {"enum": ["fetched", "expired", "refused", "pending", "unknown"]},
        "holdExpiresAtMs": {"type": "integer", "minimum": 0},
        "records": {"type": "array", "items": _STATUS_RECORD},
        "diagnostics": _DIAGNOSTICS,
    },
    "required": ["ok", "from", "records", "diagnostics"],
    "additionalProperties": False,
}

_ERROR_DATA = {
    "type": "object",
    "properties": {
        "actor": {"type": "string"},
        "claimedIdentity": {"type": "string"},
        "sender": {"type": "string"},
    },
    "additionalProperties": False,
}

_AMBIGUOUS_TARGET_DATA = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "minLength": 1},
        "candidates": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
    },
    "required": ["target", "candidates"],
    "additionalProperties": False,
}

MESSAGE_STATUS_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT, _ERROR_DATA),
    MethodError(ipc_errors.DAEMON_NOT_READY, description="message status service is not wired"),
    MethodError(ipc_errors.CALLER_NOT_AUTHORIZED, _ERROR_DATA),
    MethodError(ipc_errors.SENDER_NOT_AUTHENTICATED, _ERROR_DATA),
    MethodError(ipc_errors.SENDER_UNRESOLVED, _ERROR_DATA),
    AGENT_ADDRESS_RETIRED_ERROR,
    MethodError(ipc_errors.INVALID_AGENT_NAME),
    MethodError(ipc_errors.AMBIGUOUS_TARGET, _AMBIGUOUS_TARGET_DATA),
    MethodError(ipc_errors.STALE_SESSION),
    MethodError(ipc_errors.SESSION_SUPERSEDED),
    MethodError(ipc_errors.INVALID_SESSION_SOURCE),
)

MESSAGE_STATUS_SPEC = MethodSpec(
    name="message.status",
    owner="daemon.messaging.delivery",
    params_schema=MESSAGE_STATUS_PARAMS,
    result_schema=MESSAGE_STATUS_RESULT,
    errors=MESSAGE_STATUS_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator", "bound-agent", "adapter"),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)


def message_status(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    """Bind the existing application projection without duplicating it."""

    timeout = params.get("timeoutSeconds")
    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
        or timeout > 10
    ):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            "timeoutSeconds must be a finite number in the range (0, 10]",
        )
    try:
        return app._ipc_message_status(
            dict(params), _trusted_message_origin=trusted_message_origin
        )
    except ValueError as error:
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error


def _message_status_param_error(error: Any, params: Mapping[str, Any]) -> str:
    path = list(getattr(error, "absolute_path", ()))
    field = str(path[-1]) if path else ""
    if field == "timeoutSeconds":
        return "timeoutSeconds must be a finite number in the range (0, 10]"
    path_text = "/".join(str(part) for part in path) or "<root>"
    return f"message.status params do not match the declared schema at {path_text}"


MESSAGE_STATUS_BINDING = (MESSAGE_STATUS_SPEC, message_status, _message_status_param_error)
