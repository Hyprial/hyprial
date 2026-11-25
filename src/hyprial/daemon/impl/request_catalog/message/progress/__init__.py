"""Participant-scoped, non-consuming progress query for GUI chat."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.identity import InvalidAgentNameError
from hyprial.kernel import HttpBinding, IdentityBinding, MethodError, MethodSpec, ipc_errors

from ...address_errors import AGENT_ADDRESS_RETIRED_ERROR

from .examples import PROGRESS_LIST_EXAMPLES as PROGRESS_LIST_EXAMPLES

_STRING = {"type": "string", "minLength": 1}


def _data(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "additionalProperties": False}


_EVENT = {
    "type": "object",
    "properties": {
        "messageId": _STRING,
        "from": _STRING,
        "recipient": _STRING,
        "intent": {"const": "progress"},
        "createdAtMs": {"type": "integer"},
        "schema": {"const": "hyprial-progress-event/v1"},
        "progressEvent": {"const": True},
        "message": {"type": "string"},
        "summary": {"type": "string"},
        "deliveryId": _STRING,
        "conversationId": _STRING,
        "actor": _STRING,
        "harness": _STRING,
        "phase": _STRING,
        "seq": {"type": "integer", "minimum": 0},
        "emittedAtMs": {"type": "integer", "minimum": 1},
        "droppedSinceSeq": {"type": "integer", "minimum": 0},
        "toolCallId": {"type": "string"},
        "toolName": {"type": "string"},
        "detail": {"type": "object"},
        "terminal": {"const": True},
    },
    "required": [
        "messageId", "from", "recipient", "intent", "createdAtMs", "schema",
        "progressEvent", "message", "summary", "deliveryId", "conversationId",
        "actor", "harness", "phase", "seq", "emittedAtMs",
    ],
    "additionalProperties": False,
}

PROGRESS_LIST_SPEC = MethodSpec(
    name="progress.list",
    owner="daemon.inbox",
    params_schema={
        "type": "object",
        "properties": {
            "actor": _STRING,
            "from": {"type": ["string", "null"]},
            "sessionRef": {**_STRING, "x-hyprial-http-forbidden": True},
            "onBehalfOf": {**_STRING, "x-hyprial-http-forbidden": True},
            "deliveryId": {"type": ["string", "null"]},
            "sinceSeq": {"type": "integer", "minimum": 0},
        },
        "additionalProperties": False,
    },
    result_schema={
        "type": "object",
        "properties": {
            "ok": {"const": True},
            "events": {"type": "array", "items": _EVENT},
            "daemonEpoch": _STRING,
        },
        "required": ["ok", "events", "daemonEpoch"],
        "additionalProperties": False,
    },
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
    http_binding=HttpBinding(IdentityBinding("local-owner-user-selector", "actor"), readiness="inbox-service"),
    confirmation="none",
    ipc_param_validation="handler",
)


def progress_list(
    app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None,
) -> Any:
    return app._ipc_progress_list(
        "progress.list", dict(params), _trusted_message_origin=trusted_message_origin,
    )


CHAT_PROGRESS_BINDINGS = ((PROGRESS_LIST_SPEC, progress_list),)
