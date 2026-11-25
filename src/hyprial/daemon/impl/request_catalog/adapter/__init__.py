"""Read-only adapter methods bound to the existing daemon lifecycle projection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.kernel import (
    HttpBinding,
    IdentityBinding,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    ipc_errors,
)

_STRING = {"type": "string", "minLength": 1}
_ADAPTER = {
    "type": "object",
    "properties": {
        "id": _STRING,
        "provider": {"const": "lark"},
        "name": _STRING,
        "status": _STRING,
        "online": {"type": "boolean"},
        "configured": {"type": "boolean"},
        "desired": {"type": "boolean"},
        "processRunning": {"type": "boolean"},
        "agent": {"type": ["string", "null"]},
        "nextRetryAt": {"type": "number"},
        "retryAttempt": {"type": "integer", "minimum": 0},
        "pid": {"type": "integer", "minimum": 0},
        "error": {"type": "string"},
        "lifecycleCorrelationId": _STRING,
        "lifecycleAgeSeconds": {"type": "integer", "minimum": 0},
    },
    "required": ["id", "provider", "name", "status", "online", "configured", "desired", "processRunning"],
    "additionalProperties": False,
}
_LIST_RESULT = {
    "type": "object",
    "properties": {"ok": {"const": True}, "adapters": {"type": "array", "items": _ADAPTER}},
    "required": ["ok", "adapters"],
    "additionalProperties": False,
}
_STATUS_RESULT = {
    "type": "object",
    "properties": {"ok": {"const": True}, "adapter": _ADAPTER},
    "required": ["ok", "adapter"],
    "additionalProperties": False,
}
_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT, description="No data: the adapter selector is invalid."),
    MethodError(ipc_errors.DAEMON_NOT_READY, description="No data: the adapter service is not wired."),
)
_COMMON = {
    "owner": "daemon.adapters",
    "mode": "unary",
    "mutates": False,
    "retry": "safe",
    "audience": ("local-operator",),
    "http_exposed": True,
    "http_binding": HttpBinding(IdentityBinding("none"), readiness="lark-service"),
    "confirmation": "none",
    "idempotency": IdempotencySpec(mode="safe", scope="read-only-query"),
}
ADAPTER_LIST_SPEC = MethodSpec(
    name="adapter.list",
    errors=_ERRORS,
    params_schema={"type": "object", "properties": {}, "additionalProperties": False},
    result_schema=_LIST_RESULT,
    **_COMMON,
)
ADAPTER_STATUS_SPEC = MethodSpec(
    name="adapter.status",
    errors=(*_ERRORS, MethodError(ipc_errors.ADAPTER_NOT_FOUND, description="No data: the named adapter is absent.")),
    params_schema={
        "type": "object",
        "properties": {"name": _STRING},
        "required": ["name"],
        "additionalProperties": False,
    },
    result_schema=_STATUS_RESULT,
    **_COMMON,
)


def adapter_list(app: Any, _params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> Any:
    return app._ipc_adapter_list()


def adapter_status(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> Any:
    return app._ipc_adapter_status(dict(params))


ADAPTER_BINDINGS = (
    (ADAPTER_LIST_SPEC, adapter_list),
    (ADAPTER_STATUS_SPEC, adapter_status),
)
