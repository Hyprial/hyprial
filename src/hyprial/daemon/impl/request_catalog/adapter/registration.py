"""Register an existing app through the daemon-owned config writer.

Saving does not verify credentials or start an adapter. A saved registration
survives reload failure; callers may reload configuration, not replay the write.
The API never overwrites an existing name or returns credentials or file paths.
"""

from collections.abc import Mapping
from typing import Any

from hyprial.kernel import (
    HttpBinding, IdentityBinding, MethodError, MethodExample, MethodExampleError,
    MethodExampleSet, MethodSpec, ipc_errors,
)

_STRING = {"type": "string", "minLength": 1}
_ADAPTER = {
    "type": "object",
    "properties": {"provider": {"const": "lark"}, "name": _STRING, "appId": _STRING},
    "required": ["provider", "name", "appId"], "additionalProperties": False,
}
_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True}, "adapter": _ADAPTER, "configured": {"const": True},
        "reloaded": {"type": "boolean"}, "nextAction": {"enum": ["none", "reload"]},
    },
    "required": ["ok", "adapter", "configured", "reloaded", "nextAction"],
    "additionalProperties": False,
}
ADAPTER_REGISTER = MethodSpec(
    name="adapter.register", owner="daemon.adapters",
    params_schema={
        "type": "object", "properties": {
            "provider": {"const": "lark"}, "name": _STRING, "appId": _STRING, "appSecret": _STRING,
        }, "required": ["provider", "name", "appId", "appSecret"], "additionalProperties": False,
    },
    result_schema=_RESULT,
    errors=tuple(MethodError(code) for code in (
        ipc_errors.INVALID_ARGUMENT, ipc_errors.CALLER_NOT_AUTHORIZED, ipc_errors.DAEMON_NOT_READY,
        "ADAPTER_EXISTS", "ADAPTER_CONFIG_CONFLICT", ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
    )),
    mode="unary", mutates=True, retry="never", audience=("local-operator",),
    http_exposed=True, http_binding=HttpBinding(IdentityBinding("none"), readiness="lark-service"),
    confirmation="none",
)


def adapter_register(app: Any, params: Mapping[str, Any], *, trusted_message_origin=None):
    return app._ipc_adapter_register(dict(params), trusted_message_origin=trusted_message_origin)


ADAPTER_REGISTRATION_BINDINGS = ((ADAPTER_REGISTER, adapter_register),)
_PARAMS = {"provider": "lark", "name": "lark-main", "appId": "cli_example", "appSecret": "synthetic-only"}
_SAVED = {"ok": True, "adapter": {"provider": "lark", "name": "lark-main", "appId": "cli_example"},
          "configured": True, "reloaded": True, "nextAction": "none"}
ADAPTER_REGISTRATION_EXAMPLES = (MethodExampleSet(
    method="adapter.register", cases=(
        MethodExample(name="saved-and-loaded", kind="success", description="configured, not started or authenticated",
                      params=_PARAMS, result=_SAVED),
        MethodExample(name="saved-reload-pending", kind="success", description="configuration saved; reload needs recovery",
                      params=_PARAMS, result={**_SAVED, "reloaded": False, "nextAction": "reload"}),
        MethodExample(name="missing-secret", kind="invalid-params", description="the secret must be supplied",
                      params={key: value for key, value in _PARAMS.items() if key != "appSecret"},
                      error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "appSecret is required")),
        MethodExample(name="duplicate-name", kind="domain-error", description="registration does not overwrite",
                      params=_PARAMS, error=MethodExampleError("ADAPTER_EXISTS", "adapter already exists")),
    ), empty_not_applicable="registration is a write with an explicit saved result",
),)

_OPERATION = {"type": "object", "properties": {
    "ok": {"const": True}, "operationId": _STRING, "adapter": _STRING, "provider": {"const": "lark"}, "kind": {"const": "add"},
    "status": {"enum": ["starting", "awaiting_user", "checking", "succeeded", "denied", "expired", "cancelled", "failed", "unknown"]},
    "configured": {"type": "boolean"}, "reloaded": {"type": "boolean"}, "remoteOutcomeUnknown": {"type": "boolean"},
    "appId": _STRING, "verificationUrl": {"type": "string", "maxLength": 16384}, "expiresAt": {"type": "integer", "minimum": 0},
    "errorCode": _STRING, "nextAction": {"enum": ["none", "wait", "inspect", "reload"]},
}, "required": ["ok", "operationId", "adapter", "provider", "kind", "status", "configured", "reloaded", "remoteOutcomeUnknown", "nextAction"],
    "additionalProperties": False}
_ID = {"type": "object", "properties": {"operationId": _STRING}, "required": ["operationId"], "additionalProperties": False}
_READ = {"type": "object", "properties": {"operationId": _STRING, "name": _STRING}, "additionalProperties": False, "oneOf": [
    _ID, {"type": "object", "properties": {"name": _STRING}, "required": ["name"], "additionalProperties": False},
]}
_BEGIN = {"type": "object", "properties": {"provider": {"const": "lark"}, "name": _STRING,
          "operationKey": {"type": "string", "minLength": 1, "maxLength": 128}},
          "required": ["provider", "name", "operationKey"], "additionalProperties": False}


def _onboard_spec(name, params, *, mutates):
    return MethodSpec(
        name=name, owner="daemon.adapters", params_schema=params, result_schema=_OPERATION,
        errors=tuple(MethodError(code) for code in (ipc_errors.INVALID_ARGUMENT, ipc_errors.CALLER_NOT_AUTHORIZED, ipc_errors.DAEMON_NOT_READY,
                                                  "ADAPTER_EXISTS", "ADAPTER_OPERATION_NOT_FOUND", ipc_errors.SUBMIT_OUTCOME_UNKNOWN, "ADAPTER_ONBOARD_UNAVAILABLE")),
        mode="unary", mutates=mutates, retry="never" if mutates else "safe", audience=("local-operator",),
        http_exposed=True, http_binding=HttpBinding(IdentityBinding("none"), readiness="lark-service"), confirmation="none",
    )


ADAPTER_ONBOARD_BEGIN = _onboard_spec("adapter.onboard.begin", _BEGIN, mutates=True)
ADAPTER_ONBOARD_OPERATION = _onboard_spec("adapter.onboard.operation", _READ, mutates=False)
ADAPTER_ONBOARD_CANCEL = _onboard_spec("adapter.onboard.cancel", _ID, mutates=True)


def adapter_onboard_begin(app, params, *, trusted_message_origin=None):
    return app._ipc_adapter_onboard(dict(params), action="begin", trusted_message_origin=trusted_message_origin)


def adapter_onboard_operation(app, params, *, trusted_message_origin=None):
    return app._ipc_adapter_onboard(dict(params), action="operation", trusted_message_origin=trusted_message_origin)


def adapter_onboard_cancel(app, params, *, trusted_message_origin=None):
    return app._ipc_adapter_onboard(dict(params), action="cancel", trusted_message_origin=trusted_message_origin)


ADAPTER_REGISTRATION_BINDINGS += ((ADAPTER_ONBOARD_BEGIN, adapter_onboard_begin),
                                (ADAPTER_ONBOARD_OPERATION, adapter_onboard_operation), (ADAPTER_ONBOARD_CANCEL, adapter_onboard_cancel))
_INITIAL = {"ok": True, "operationId": "create-example", "adapter": "lark-new", "provider": "lark", "kind": "add", "status": "starting",
            "configured": False, "reloaded": False, "remoteOutcomeUnknown": False, "nextAction": "wait"}
ADAPTER_REGISTRATION_EXAMPLES += tuple(MethodExampleSet(method=spec.name, cases=(
    MethodExample(name="receipt", kind="success", description="creation receipt is not a completed configuration", params=params,
                  result={**_INITIAL, **({"status": "cancelled", "remoteOutcomeUnknown": True, "nextAction": "inspect"} if spec is ADAPTER_ONBOARD_CANCEL else {})}),
    MethodExample(name="missing-identity", kind="invalid-params", description="operation identity required", params={},
                  error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "operation parameters required")),
    MethodExample(name="unavailable", kind="domain-error", description="no automatic replay", params=params,
                  error=MethodExampleError(ipc_errors.SUBMIT_OUTCOME_UNKNOWN, "app creation receipt unavailable")),
), empty_not_applicable="an absent operation returns ADAPTER_OPERATION_NOT_FOUND") for spec, params in (
    (ADAPTER_ONBOARD_BEGIN, {"provider": "lark", "name": "lark-new", "operationKey": "example-key"}),
    (ADAPTER_ONBOARD_OPERATION, {"operationId": "create-example"}), (ADAPTER_ONBOARD_CANCEL, {"operationId": "create-example"}),
))
