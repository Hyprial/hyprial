"""The registered declaration for the daemon's consuming inbox batch read.

The handler deliberately delegates to the existing application authority.  The
catalog owns only the public contract and registry binding; capture, cursor,
spill and session semantics remain in the shared ``_ipc`` implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.kernel import (
    IdempotencySpec,
    MethodError,
    MethodSpec,
    ipc_errors,
)

from ..address_errors import AGENT_ADDRESS_RETIRED_ERROR

_PARAMS = {
    "type": "object",
    "properties": {
        "actor": {"type": "string", "minLength": 1},
        "from": {"type": "string", "minLength": 1},
        "sessionRef": {"type": "string", "minLength": 1},
        "onBehalfOf": {"type": "string", "minLength": 1},
        "cursor": {"type": "string"},
        "afterCursor": {"type": "string"},
    },
    "anyOf": [
        {"type": "object", "required": ["actor"]},
        {"type": "object", "required": ["from"]},
    ],
    "additionalProperties": False,
}

_ORIGIN = {
    "type": "object",
    "properties": {
        "via": {"type": "string"},
        "chatKind": {"enum": ["dm", "group"]},
        "provider": {"type": "string"},
        "platformMessageId": {"type": "string"},
        "platformCreatedAt": {"type": "string"},
        "onBehalfOf": {"type": "string"},
        "mechanism": {"type": "string"},
        "sender": {
            "type": "object",
            "properties": {
                "kind": {"type": ["string", "null"]},
                "platformId": {"type": "string"},
                "displayName": {"type": ["string", "null"]},
            },
            "required": ["platformId"],
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}

_ITEM = {
    "type": "object",
    "properties": {
        "messageId": {"type": "string"},
        "deliveryId": {"type": "string"},
        "conversationId": {"type": "string"},
        "from": {"type": "string"},
        "to": {"type": "string"},
        "intent": {"type": "string"},
        "kind": {"type": "string"},
        "createdAtMs": {"type": "integer"},
        "summary": {"type": "string"},
        "body": {"type": ["string", "null"]},
        "bodyBytes": {"type": "integer", "minimum": 0},
        "origin": _ORIGIN,
        "class": {"type": "string"},
        "expectedAction": {"type": "string"},
    },
    "required": ["messageId", "deliveryId", "conversationId", "from", "to", "intent", "kind", "summary", "body", "bodyBytes", "origin"],
    "additionalProperties": False,
}

_DELIVERY = {
    "type": "object",
    "properties": {
        "mode": {"enum": ["inline", "spill"]},
        "text": {"type": "string"},
        "spillPath": {"type": ["string", "null"]},
        "spillBytes": {"type": ["integer", "null"], "minimum": 0},
        "spillSha256": {"type": ["string", "null"]},
    },
    "required": ["mode", "text", "spillPath", "spillBytes", "spillSha256"],
    "additionalProperties": False,
}

_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "schemaVersion": {"const": 1},
        "format": {"const": "inbox-batch-v1"},
        "cursor": {"type": "string"},
        "daemonEpoch": {"type": "string"},
        "actor": {"type": "string"},
        "itemCount": {"type": "integer", "minimum": 0},
        "totalBodyBytes": {"type": "integer", "minimum": 0},
        "inlineLimitBytes": {"type": "integer", "minimum": 0},
        "items": {"type": "array", "items": _ITEM},
        "delivery": _DELIVERY,
    },
    "required": ["ok", "schemaVersion", "format", "cursor", "daemonEpoch", "actor", "itemCount", "totalBodyBytes", "inlineLimitBytes", "items", "delivery"],
    "additionalProperties": False,
}

_CLAIMED_IDENTITY = {
    "type": "object",
    "properties": {"claimedIdentity": {"type": "string"}},
    "required": ["claimedIdentity"],
    "additionalProperties": False,
}

_SENDER = {
    "type": "object",
    "properties": {"sender": {"type": "string"}},
    "required": ["sender"],
    "additionalProperties": False,
}

_AMBIGUOUS_TARGET = {
    "type": "object",
    "properties": {
        "target": {"type": "string"},
        "candidates": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["target", "candidates"],
    "additionalProperties": False,
}

_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT),
    MethodError(ipc_errors.CALLER_NOT_AUTHORIZED),
    MethodError(ipc_errors.INBOX_BATCH_SPILL_UNAVAILABLE),
    MethodError(ipc_errors.INVALID_AGENT_NAME),
    MethodError(ipc_errors.SENDER_UNRESOLVED, _SENDER),
    AGENT_ADDRESS_RETIRED_ERROR,
    MethodError(ipc_errors.AMBIGUOUS_TARGET, _AMBIGUOUS_TARGET),
    MethodError(ipc_errors.SENDER_NOT_AUTHENTICATED, _CLAIMED_IDENTITY),
    MethodError(ipc_errors.STALE_SESSION),
    MethodError(ipc_errors.SESSION_SUPERSEDED),
    MethodError(ipc_errors.INVALID_SESSION_SOURCE),
)

PENDING_BATCH_SPEC = MethodSpec(
    name="message.pending.batch",
    owner="daemon.messaging.inbox",
    params_schema=_PARAMS,
    result_schema=_RESULT,
    errors=_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator", "bound-agent", "adapter", "internal"),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="consuming-inbox-capture"),
)


def pending_batch(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> Any:
    """Invoke the pre-existing consuming-read authority with trusted origin."""

    return app._ipc_message_pending_batch(  # noqa: SLF001
        "message.pending.batch",
        dict(params),
        _trusted_message_origin=trusted_message_origin,
    )


PENDING_BATCH_BINDING = (PENDING_BATCH_SPEC, pending_batch)
