"""Operator-only temporary credential testing; no Key, model, agent or session writes.

A successful request can bill the selected model service, hence mutates/never.
The daemon accepts at most four concurrent probes and never queues another one.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from typing import Any

from hyprial.identity import ProviderKeys, SecretResolutionError, SecretResolver

from hyprial.kernel import (
    DaemonRequestError, HttpBinding, IdentityBinding, IdempotencySpec,
    MethodError, MethodExample, MethodExampleError, MethodExampleSet, MethodSpec, ipc_errors,
)

METHOD = "agent.secret.provider-test"
_CAPACITY = threading.BoundedSemaphore(4)
_OUTCOMES = ["passed", "authentication-rejected", "rate-limited", "redirect-refused",
             "request-rejected", "upstream-error", "invalid-response", "timeout", "unreachable"]

SPEC = MethodSpec(
    name=METHOD, owner="daemon.agent.secret", mode="unary", mutates=True, retry="never",
    audience=("local-operator",), http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")), confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="temporary-model-probe"),
    params_schema={"type": "object", "properties": {
        "connection": {"type": "object", "properties": {
            "entryId": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"},
            "fieldName": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"},
            "connectionRef": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
            "configRevision": {"type": "integer", "minimum": 1}},
            "required": ["entryId", "fieldName", "connectionRef", "configRevision"],
            "additionalProperties": False},
        "timeoutSeconds": {"type": "integer", "minimum": 1, "maximum": 60},
        "provider": {"type": "string", "minLength": 1, "maxLength": 128},
        "secret": {"type": "string", "writeOnly": True, "minLength": 1, "maxLength": 16384,
                   "pattern": "^[!-~]+$"},
        "model": {"type": "string", "minLength": 1, "maxLength": 256},
        "endpoint": {"type": "string", "minLength": 1, "maxLength": 2048},
        "protocol": {"type": "string", "enum": ["openai-completions", "anthropic-messages"]}},
        "required": ["model"], "additionalProperties": False,
        "oneOf": [
            {"type": "object", "required": ["provider", "secret"],
             "not": {"type": "object", "required": ["connection"]}},
            {"type": "object", "required": ["connection"], "not": {"anyOf": [
                {"type": "object", "required": ["provider"]},
                {"type": "object", "required": ["secret"]},
                {"type": "object", "required": ["endpoint"]},
                {"type": "object", "required": ["protocol"]}]}}]},
    result_schema={"type": "object", "properties": {
        "ok": {"const": True}, "outcome": {"type": "string", "enum": _OUTCOMES},
        "phase": {"const": "probe"}, "reason": {"const": "deadline-exceeded"},
        "httpStatus": {"type": ["integer", "null"], "minimum": 100, "maximum": 599},
        "latencyMs": {"type": "integer", "minimum": 0}},
        "required": ["ok", "outcome", "httpStatus", "latencyMs"], "additionalProperties": False,
        "dependentRequired": {"phase": ["reason"], "reason": ["phase"]},
        "if": {"type": "object", "required": ["phase"]},
        "then": {"type": "object", "properties": {"outcome": {"const": "timeout"}}}},
    errors=tuple(MethodError(code, description=description) for code, description in (
        (ipc_errors.INVALID_ARGUMENT, "Invalid temporary probe input."),
        (ipc_errors.CALLER_NOT_AUTHORIZED, "Only a local operator may test a credential."),
        (ipc_errors.RATE_LIMITED, "All temporary probe slots are occupied."),
        (ipc_errors.DAEMON_ERROR, "The probe could not be completed."))) + (
        MethodError(ipc_errors.MODEL_CONFIGURATION_CONFLICT, {
            "type": "object", "properties": {"phase": {"const": "bind"},
                "reason": {"const": "revision-mismatch"}},
            "required": ["phase", "reason"], "additionalProperties": False,
        }, description="The selected connection changed."),
        MethodError(ipc_errors.MODEL_AUTH_UNAVAILABLE, {
            "type": "object", "properties": {"phase": {"const": "authorize"},
                "reason": {"enum": ["missing", "unreadable"]}},
            "required": ["phase", "reason"], "additionalProperties": False,
        }, description="The selected connection cannot be read.")),
)


def admit(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> None:
    from hyprial.daemon.impl.provider_keys.ipc import preauthorize
    preauthorize(METHOD, app, params, trusted_message_origin=trusted_message_origin)


def handle(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> dict:
    admit(app, params, trusted_message_origin=trusted_message_origin)
    if not _CAPACITY.acquire(blocking=False):
        raise DaemonRequestError(ipc_errors.RATE_LIMITED, "Credential probe capacity is occupied.")
    try:
        from hyprial.daemon.impl.model_probe import probe
        if "connection" in params:
            model = params["model"]
            if not model or model != model.strip():
                raise ValueError("invalid model ID")
            from hyprial.daemon.impl.provider_keys.legacy import _gate_closed
            if not _gate_closed(app):
                raise SecretResolutionError("authority", "connection", "probe")
            selected = params["connection"]
            resolver = SecretResolver(app.hyprial_home, app._agent_registry)
            prepared = resolver.model_connection_probe_input(
                selected["entryId"], field_name=selected["fieldName"],
                connection_ref=selected["connectionRef"], config_revision=selected["configRevision"],
            )
            prepared["model"] = params["model"]
            if "timeoutSeconds" in params:
                prepared["timeoutSeconds"] = params["timeoutSeconds"]
            result = asyncio.run(probe(prepared))
        else:
            result = asyncio.run(probe(params))
        if result["outcome"] == "timeout":
            result = {**result, "phase": "probe", "reason": "deadline-exceeded"}
        return result
    except SecretResolutionError as error:
        code = (ipc_errors.MODEL_CONFIGURATION_CONFLICT if error.category == "revision-mismatch"
                else ipc_errors.MODEL_AUTH_UNAVAILABLE)
        data = ({"phase": "bind", "reason": "revision-mismatch"}
                if code == ipc_errors.MODEL_CONFIGURATION_CONFLICT
                else {"phase": "authorize", "reason": "missing" if error.category == "missing" else "unreadable"})
        raise DaemonRequestError(code, "Selected model connection is unavailable or changed.", data=data) from None
    except ProviderKeys.Error:
        raise DaemonRequestError(ipc_errors.MODEL_AUTH_UNAVAILABLE,
            "Selected model connection authority is unavailable.",
            data={"phase": "authorize", "reason": "unreadable"}) from None
    except ValueError:
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "Invalid credential probe input.") from None
    except Exception:
        raise DaemonRequestError(ipc_errors.DAEMON_ERROR, "Credential probe could not be completed.") from None
    finally:
        _CAPACITY.release()


EXAMPLES = MethodExampleSet(
    method=METHOD, empty_not_applicable="A probe always reports its attempted model request outcome.",
    cases=(
        MethodExample(name="model-replied", kind="success", description="Synthetic protocol response, not a saved Key.",
                      params={"provider": "deepseek", "secret": "example-not-a-credential", "model": "example-model"},
                      result={"ok": True, "outcome": "passed", "httpStatus": 200, "latencyMs": 40}),
        MethodExample(name="invalid-input", kind="invalid-params", description="No model was selected.",
                      params={"provider": "deepseek", "secret": "example-not-a-credential"},
                      error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "Invalid credential probe input.")),
        MethodExample(name="capacity", kind="domain-error", description="No request is sent when the bound is reached.",
                      params={"provider": "deepseek", "secret": "example-not-a-credential", "model": "example-model"},
                      error=MethodExampleError(ipc_errors.RATE_LIMITED, "Credential probe capacity is occupied.")),
        MethodExample(name="saved-connection-replied", kind="success", description="Exact saved connection revision and manually supplied model ID.",
                      params={"connection": {"entryId": "example-custom", "fieldName": "key",
                              "connectionRef": "0123456789abcdef0123456789abcdef", "configRevision": 1},
                              "model": "hy4-flash", "timeoutSeconds": 30},
                      result={"ok": True, "outcome": "passed", "httpStatus": 200, "latencyMs": 40}),
        MethodExample(name="saved-connection-timeout", kind="success", description="The probe completed with an unverified timeout, not a rejected credential.",
                      params={"connection": {"entryId": "example-custom", "fieldName": "key",
                              "connectionRef": "0123456789abcdef0123456789abcdef", "configRevision": 1},
                              "model": "hy4-flash", "timeoutSeconds": 30},
                      result={"ok": True, "outcome": "timeout", "httpStatus": None, "latencyMs": 30000,
                              "phase": "probe", "reason": "deadline-exceeded"}),
    ),
)
