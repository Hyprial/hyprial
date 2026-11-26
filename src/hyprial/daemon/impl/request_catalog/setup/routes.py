"""Owner-selected model service configuration over the existing Secret store.

This is a distinct strict method: older writers ignore unknown extensions.
The operator gate runs before validation; saved values never enter receipts.
Versioned connections do not enable launch until their separate binding admission exists.
"""

from typing import Any, Mapping

from hyprial.identity import ProviderKeys, SecretResolutionError, SecretResolver
from hyprial.kernel import (
    DaemonRequestError, HttpBinding, IdentityBinding, IdempotencySpec,
    MethodError, MethodExample, MethodExampleError, MethodExampleSet, MethodSpec, ipc_errors,
)

from .specs import MODEL_CONNECTION_INPUT, MODEL_CONNECTIONS, MODEL_ROUTES, _ERRORS

METHOD = "agent.secret.provider-configure"
_NAME = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"}
SPEC = MethodSpec(
    name=METHOD, owner="daemon.agent.secret",
    params_schema={"type": "object", "properties": {
        "entryId": _NAME, "fieldName": _NAME,
        "connection": MODEL_CONNECTION_INPUT,
        "expectedRevision": {"type": "integer", "minimum": 0},
        "endpoint": {"type": "string", "minLength": 1, "maxLength": 2048},
        "protocol": {"const": "openai-completions"}, "modelProvider": {"const": "deepseek"},
        "createOnly": {"type": "boolean", "default": False},
        "value": {"type": "string", "minLength": 1, "maxLength": 16384,
                  "not": {"type": "string", "pattern": "[\\r\\n]"}, "writeOnly": True}},
        "required": ["entryId", "fieldName"],
        "oneOf": [
            {"type": "object", "required": ["endpoint", "protocol", "modelProvider"],
             "not": {"anyOf": [{"type": "object", "required": ["connection"]}, {"type": "object", "required": ["expectedRevision"]}]}},
            {"type": "object", "required": ["connection", "expectedRevision"],
             "not": {"anyOf": [{"type": "object", "required": ["endpoint"]}, {"type": "object", "required": ["protocol"]},
                                {"type": "object", "required": ["modelProvider"]}]}}],
        "additionalProperties": False},
    result_schema={"type": "object", "properties": {"ok": {"const": True},
        "entryId": _NAME, "fieldNames": {"type": "array", "items": _NAME}, "routes": MODEL_ROUTES, "connections": MODEL_CONNECTIONS},
        "required": ["ok", "entryId", "fieldNames", "routes"], "additionalProperties": False},
    errors=_ERRORS + (MethodError(ipc_errors.MODEL_CONFIGURATION_INVALID, {
        "type": "object", "properties": {"phase": {"const": "validate"},
            "reason": {"const": "invalid-connection"}},
        "required": ["phase", "reason"], "additionalProperties": False,
    }, description="The connection or its saved Key is unusable."), MethodError(ipc_errors.MODEL_CONFIGURATION_CONFLICT, {
        "type": "object", "properties": {"phase": {"const": "bind"},
            "reason": {"const": "revision-mismatch"}},
        "required": ["phase", "reason"], "additionalProperties": False,
    }, description="The connection revision changed; reread before saving."),), mode="unary", mutates=True, retry="never", audience=("local-operator",),
    http_exposed=True, http_binding=HttpBinding(IdentityBinding("none")), confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="model-service-configuration"),
)


def admit(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> None:
    from hyprial.daemon.impl.provider_keys.ipc import preauthorize

    preauthorize(METHOD, app, params, trusted_message_origin=trusted_message_origin)


def handle(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> dict[str, Any]:
    from hyprial.daemon.impl.provider_keys.legacy import _gate_closed

    admit(app, params, trusted_message_origin=trusted_message_origin)
    try:
        if not _gate_closed(app):
            raise SecretResolutionError("authority", "configuration", "write")
        resolver = SecretResolver(app.hyprial_home, app._agent_registry)
        if "connection" in params:
            resolver.configure_model_connection(
                params["entryId"], field_name=params["fieldName"], connection=params["connection"],
                expected_revision=params["expectedRevision"], value=params.get("value"),
                create_only=params.get("createOnly", False),
            )
        else:
            resolver.configure_model_service(
                params["entryId"], field_name=params["fieldName"], endpoint=params["endpoint"],
                protocol=params["protocol"], vendor_id=params["modelProvider"], value=params.get("value"),
                create_only=params.get("createOnly", False),
            )
        return {"ok": True, **resolver.model_service_metadata(params["entryId"])}
    except SecretResolutionError as error:
        if error.category == "revision-mismatch":
            raise DaemonRequestError(ipc_errors.MODEL_CONFIGURATION_CONFLICT,
                "Model connection changed; reload before saving.",
                data={"phase": "bind", "reason": "revision-mismatch"}) from None
        if error.category == "exists":
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT,
                                     "Entry already exists; choose another name.") from None
        raise DaemonRequestError(ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
                                 "Model service configuration could not be saved.") from None
    except (ProviderKeys.Error, OSError):
        raise DaemonRequestError(ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
                                 "Model service configuration could not be saved.") from None
    except (TypeError, ValueError):
        if "connection" in params:
            raise DaemonRequestError(ipc_errors.MODEL_CONFIGURATION_INVALID,
                "Invalid model connection configuration.",
                data={"phase": "validate", "reason": "invalid-connection"}) from None
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "Invalid model service configuration.") from None


_PARAMS = {"entryId": "example-model", "fieldName": "key", "endpoint": "https://model.example/v1",
           "protocol": "openai-completions", "modelProvider": "deepseek", "value": "synthetic-example"}
_CONNECTION = {"version": 1, "endpoint": "https://model.example/v1",
               "protocol": "openai-completions", "profile": "openai-chat-completions/v1",
               "preset": {"id": "custom", "version": 1}}
_CONNECTION_PARAMS = {"entryId": "example-custom", "fieldName": "key", "connection": _CONNECTION,
                      "expectedRevision": 0, "value": "synthetic-example"}
EXAMPLES = (MethodExampleSet(
    method=METHOD, empty_not_applicable="Configuration acknowledges one saved non-secret route.",
    cases=(
        MethodExample(name="configured", kind="success", description="Synthetic owner-selected route.",
            params=_PARAMS, result={"ok": True, "entryId": "example-model", "fieldNames": ["key"],
                "routes": {"key": {"endpoint": _PARAMS["endpoint"], "protocol": "openai-completions", "vendorId": "deepseek"}}}),
        MethodExample(name="unknown-param", kind="invalid-params", description="Unknown extensions are refused.",
            params={**_PARAMS, "unknown": True}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "Invalid model service configuration.")),
        MethodExample(name="unavailable", kind="domain-error", description="No other authority is bypassed.",
            params=_PARAMS, error=MethodExampleError(ipc_errors.AGENT_SECRET_RESOLUTION_FAILED, "Model service configuration could not be saved.")),
        MethodExample(name="connection-configured", kind="success", description="Explicit custom connection; no grant or launch is created.",
            params=_CONNECTION_PARAMS, result={"ok": True, "entryId": "example-custom", "fieldNames": ["key"],
                "routes": {}, "connections": {"key": {**_CONNECTION, "connectionRef": "0123456789abcdef0123456789abcdef", "configRevision": 1}}}),
        MethodExample(name="connection-rotated", kind="success", description="CAS rotation preserves the exact connection identity and increases its revision.",
            params={**_CONNECTION_PARAMS, "expectedRevision": 1}, result={"ok": True, "entryId": "example-custom", "fieldNames": ["key"],
                "routes": {}, "connections": {"key": {**_CONNECTION, "connectionRef": "0123456789abcdef0123456789abcdef", "configRevision": 2}}}),
        MethodExample(name="connection-conflict", kind="domain-error", description="A stale edit never overwrites the stored credential.",
            params={**_CONNECTION_PARAMS, "expectedRevision": 1},
            error=MethodExampleError(ipc_errors.MODEL_CONFIGURATION_CONFLICT, "Model connection changed; reload before saving.",
                                     data={"phase": "bind", "reason": "revision-mismatch"})),
    ),
),)
