"""Scope observation is independent of authorization writes and adapter lists.

The existing inventory validates tenant grants and diagnoses capability IDs.
An unavailable observation remains unconfirmed, not an empty authorization.
HTTP socket operations have a two-second timeout; this is not a total deadline.
"""

from collections.abc import Mapping
from typing import Any
from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.adapters.lark.credentials.scopes import CAPABILITY_IDS

from hyprial.kernel import (
    HttpBinding, IdentityBinding, MethodError, MethodExample, MethodExampleError,
    MethodExampleSet, MethodSpec,
)

_NAME = {"type": "string", "minLength": 1}
_CAPABILITIES = {"type": "array", "items": _NAME}
_STATUS = {
    "type": "object", "properties": {
        "ok": {"const": True}, "adapter": _NAME, "appId": {"type": ["string", "null"]},
        "state": {"enum": ["authorized", "unauthorized", "unconfigured", "unconfirmed"]},
        "capabilities": _CAPABILITIES, "missingCapabilities": _CAPABILITIES,
        "checkedAt": {"type": "integer", "minimum": 0}, "reason": _NAME,
    },
    "required": ["ok", "adapter", "appId", "state", "capabilities", "missingCapabilities", "checkedAt"],
    "additionalProperties": False,
}
ADAPTER_AUTHORIZATION_STATUS = MethodSpec(
    name="adapter.authorization.status", owner="daemon.adapters",
    params_schema={"type": "object", "properties": {"name": _NAME}, "required": ["name"], "additionalProperties": False},
    result_schema=_STATUS,
    errors=tuple(MethodError(code) for code in (ipc_errors.INVALID_ARGUMENT, ipc_errors.CALLER_NOT_AUTHORIZED, ipc_errors.DAEMON_NOT_READY, ipc_errors.ADAPTER_NOT_FOUND)),
    mode="unary", mutates=False, retry="safe", audience=("local-operator",),
    http_exposed=True, http_binding=HttpBinding(IdentityBinding("none"), readiness="lark-service"), confirmation="none",
)


def adapter_authorization_status(app: Any, params: Mapping[str, Any], *, trusted_message_origin=None):
    return app._ipc_adapter_authorization_status(dict(params), trusted_message_origin=trusted_message_origin)


ADAPTER_AUTHORIZATION_BINDINGS = ((ADAPTER_AUTHORIZATION_STATUS, adapter_authorization_status),)
_KNOWN = {"ok": True, "adapter": "lark-main", "appId": "cli_example", "state": "authorized",
          "capabilities": ["message-send", "receive-p2p", "receive-group-mention"], "missingCapabilities": [], "checkedAt": 1700000000000}
ADAPTER_AUTHORIZATION_EXAMPLES = (MethodExampleSet(
    method="adapter.authorization.status", cases=(
        MethodExample(name="authorized", kind="success", description="observed tenant grants", params={"name": "lark-main"}, result=_KNOWN),
        MethodExample(name="empty-grants", kind="empty", description="observed no tenant grants", params={"name": "lark-main"},
                      result={**_KNOWN, "state": "unauthorized", "capabilities": [], "missingCapabilities": _KNOWN["capabilities"]},
                      reason="a configured app may have no tenant grants"),
        MethodExample(name="unavailable", kind="success", description="failed observation is not unauthorized", params={"name": "lark-main"},
                      result={**_KNOWN, "state": "unconfirmed", "capabilities": [], "reason": "SCOPE_QUERY_FAILED"}),
        MethodExample(name="missing-name", kind="invalid-params", description="adapter name required", params={},
                      error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "name is required")),
        MethodExample(name="not-configured", kind="domain-error", description="no adapter configured", params={"name": "missing"},
                      error=MethodExampleError(ipc_errors.ADAPTER_NOT_FOUND, "adapter not found")),
    ),
),)

_ERRORS = tuple(MethodError(code) for code in (
    ipc_errors.INVALID_ARGUMENT, ipc_errors.CALLER_NOT_AUTHORIZED, ipc_errors.DAEMON_NOT_READY, ipc_errors.ADAPTER_NOT_FOUND,
    "ADAPTER_OPERATION_NOT_FOUND", ipc_errors.SUBMIT_OUTCOME_UNKNOWN, "ADAPTER_AUTHORIZATION_UNAVAILABLE",
))
_OPERATION = {
    "type": "object", "properties": {
        "ok": {"const": True}, "operationId": _NAME, "adapter": _NAME,
        "provider": {"const": "lark"}, "kind": {"const": "authorization"},
        "status": {"enum": ["starting", "awaiting_user", "checking", "succeeded", "denied", "expired", "cancelled", "failed", "unknown"]},
        "authorization": {**_STATUS, "properties": {key: value for key, value in _STATUS["properties"].items() if key != "ok"},
                          "required": [key for key in _STATUS["required"] if key != "ok"]},
        "verificationUrl": {"type": "string", "maxLength": 16384}, "expiresAt": {"type": "integer", "minimum": 0}, "errorCode": _NAME,
    }, "required": ["ok", "operationId", "adapter", "provider", "kind", "status", "authorization"], "additionalProperties": False,
}
_BEGIN_PARAMS = {"type": "object", "properties": {
    "name": _NAME, "operationKey": {"type": "string", "minLength": 1, "maxLength": 128},
    "capabilities": {"type": "array", "items": {"enum": list(CAPABILITY_IDS)}, "minItems": 3, "maxItems": len(CAPABILITY_IDS), "uniqueItems": True},
}, "required": ["name", "operationKey", "capabilities"], "additionalProperties": False}
_ID_PARAMS = {"type": "object", "properties": {"operationId": _NAME}, "required": ["operationId"], "additionalProperties": False}
_READ_PARAMS = {"type": "object", "properties": {"operationId": _NAME, "name": _NAME}, "additionalProperties": False, "oneOf": [
    _ID_PARAMS,
    {"type": "object", "properties": {"name": _NAME}, "required": ["name"], "additionalProperties": False},
]}


def _operation_spec(name, params, *, mutates):
    return MethodSpec(
        name=name, owner="daemon.adapters", params_schema=params, result_schema=_OPERATION, errors=_ERRORS,
        mode="unary", mutates=mutates, retry="never" if mutates else "safe", audience=("local-operator",),
        http_exposed=True, http_binding=HttpBinding(IdentityBinding("none"), readiness="lark-service"), confirmation="none",
    )


ADAPTER_AUTHORIZATION_BEGIN = _operation_spec("adapter.authorization.begin", _BEGIN_PARAMS, mutates=True)
ADAPTER_AUTHORIZATION_OPERATION = _operation_spec("adapter.authorization.operation", _READ_PARAMS, mutates=False)
ADAPTER_AUTHORIZATION_CANCEL = _operation_spec("adapter.authorization.cancel", _ID_PARAMS, mutates=True)


def adapter_authorization_begin(app, params, *, trusted_message_origin=None):
    return app._ipc_adapter_authorization_operation(dict(params), action="begin", trusted_message_origin=trusted_message_origin)


def adapter_authorization_operation(app, params, *, trusted_message_origin=None):
    return app._ipc_adapter_authorization_operation(dict(params), action="operation", trusted_message_origin=trusted_message_origin)


def adapter_authorization_cancel(app, params, *, trusted_message_origin=None):
    return app._ipc_adapter_authorization_operation(dict(params), action="cancel", trusted_message_origin=trusted_message_origin)


ADAPTER_AUTHORIZATION_BINDINGS += (
    (ADAPTER_AUTHORIZATION_BEGIN, adapter_authorization_begin),
    (ADAPTER_AUTHORIZATION_OPERATION, adapter_authorization_operation),
    (ADAPTER_AUTHORIZATION_CANCEL, adapter_authorization_cancel),
)
_INITIAL = {"ok": True, "operationId": "auth-example", "adapter": "lark-main", "provider": "lark", "kind": "authorization",
            "status": "starting", "authorization": {key: value for key, value in _KNOWN.items() if key != "ok"}}
_BEGIN = {"name": "lark-main", "operationKey": "example-key", "capabilities": ["message-send", "receive-p2p", "receive-group-mention"]}
ADAPTER_AUTHORIZATION_EXAMPLES += tuple(MethodExampleSet(method=spec.name, cases=(
    MethodExample(name="accepted", kind="success", description="operation receipt is not a platform grant",
                  params=params, result={**_INITIAL, "status": "cancelled" if spec is ADAPTER_AUTHORIZATION_CANCEL else "starting"}),
    MethodExample(name="invalid", kind="invalid-params", description="required operation identity missing", params={},
                  error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "operation parameters required")),
    MethodExample(name="unavailable", kind="domain-error", description="receipt unavailable; never replay automatically", params=params,
                  error=MethodExampleError(ipc_errors.SUBMIT_OUTCOME_UNKNOWN, "authorization receipt unavailable")),
), empty_not_applicable="an absent authorization operation returns ADAPTER_OPERATION_NOT_FOUND") for spec, params in (
    (ADAPTER_AUTHORIZATION_BEGIN, _BEGIN),
    (ADAPTER_AUTHORIZATION_OPERATION, {"operationId": "auth-example"}),
    (ADAPTER_AUTHORIZATION_CANCEL, {"operationId": "auth-example"}),
))
