"""The existing adapter lifecycle commands through the H-03 write profile."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.daemon.impl.adapters.lark.contracts.lifecycle import ADAPTER_START_TIMEOUT
from hyprial.kernel import HttpBinding, IdentityBinding, MethodError, MethodSpec, ipc_errors

from . import _ADAPTER, _ERRORS, _STRING

_VALUE = {"type": ["string", "number", "boolean", "null", "array", "object"]}
_PARAMS = {
    "type": "object",
    "properties": {"name": _STRING},
    "required": ["name"],
    "additionalProperties": _VALUE,
}
_DETACHED = {
    "type": "object",
    "properties": {"name": _STRING, "status": {"const": "stopped"}, "configured": {"const": False}},
    "required": ["name", "status", "configured"],
    "additionalProperties": False,
}
_RELOAD_FIELDS = {
    field: {"type": "array", "items": _STRING}
    for field in ("added", "updated", "removed", "removedRunning")
}
_RELOAD_RESULT = {
    "type": "object",
    "properties": {"ok": {"const": True}, **_RELOAD_FIELDS},
    "required": ["ok", *_RELOAD_FIELDS],
    "additionalProperties": False,
}
_WRITE = {
    "owner": "daemon.adapters", "mode": "unary", "mutates": True, "retry": "never",
    "audience": ("local-operator",), "http_exposed": True,
    "http_binding": HttpBinding(IdentityBinding("none"), readiness="lark-service"),
    "confirmation": "none",
}

ADAPTER_START_SPEC = MethodSpec(
    name="adapter.start", params_schema=_PARAMS,
    result_schema={
        "type": "object",
        "properties": {"ok": {"const": True}, "changed": {"type": "boolean"}, "adapter": _ADAPTER},
        "required": ["ok", "changed", "adapter"], "additionalProperties": False,
    },
    errors=(*_ERRORS, MethodError(ipc_errors.ADAPTER_NOT_FOUND),
            MethodError("ADAPTER_START_FAILED"), MethodError(ADAPTER_START_TIMEOUT)),
    **_WRITE,
)
ADAPTER_STOP_SPEC = MethodSpec(
    name="adapter.stop", params_schema=_PARAMS,
    result_schema={
        "type": "object",
        "properties": {"ok": {"const": True}, "changed": {"type": "boolean"},
                       "adapter": {"oneOf": [_ADAPTER, _DETACHED]}},
        "required": ["ok", "changed", "adapter"], "additionalProperties": False,
    },
    errors=(*_ERRORS, MethodError(ipc_errors.ADAPTER_NOT_FOUND)),
    **_WRITE,
)
ADAPTER_RELOAD_SPEC = MethodSpec(
    name="adapter.reload",
    params_schema={"type": "object", "properties": {}, "additionalProperties": False},
    result_schema=_RELOAD_RESULT,
    errors=(*_ERRORS, MethodError(ipc_errors.ADAPTER_RELOAD_FAILED, {
        "type": "object", "properties": {"reloaded": {
            "type": "object", "properties": _RELOAD_FIELDS,
            "required": list(_RELOAD_FIELDS), "additionalProperties": False,
        }}, "required": ["reloaded"], "additionalProperties": False,
    })),
    **_WRITE,
)


def adapter_start(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> Any:
    return app._ipc_adapter_start(dict(params))


def adapter_stop(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> Any:
    return app._ipc_adapter_stop(dict(params))


def adapter_reload(app: Any, _params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> Any:
    return app._ipc_adapter_reload()


ADAPTER_LIFECYCLE_BINDINGS = (
    (ADAPTER_START_SPEC, adapter_start),
    (ADAPTER_STOP_SPEC, adapter_stop),
    (ADAPTER_RELOAD_SPEC, adapter_reload),
)
