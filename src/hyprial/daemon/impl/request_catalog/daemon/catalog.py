"""The read-only ``daemon.catalog`` method: declared methods plus live state.

The handler projects the same ``MethodSpec`` declarations the process froze at
assembly time and adds only dynamic availability.  It never calls a business
handler, reads inbox/links/PAC state, or probes by invoking a method.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial import __version__
from hyprial.kernel import (
    MethodError,
    MethodSpec,
    export_catalog,
    ipc_errors,
)

from .readiness import READINESS_PROBES

JsonObject = dict[str, Any]

_HTTP_BINDING = {
    "oneOf": [
        {"type": "null"},
        {
            "type": "object",
            "properties": {
                "identity": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["none", "local-operator-selector"],
                        },
                        "param": {"type": ["string", "null"]},
                    },
                    "required": ["mode", "param"],
                    "additionalProperties": False,
                },
                "readiness": {"type": ["string", "null"]},
                "readOnlyParamsSchema": {"type": "object"},
            },
            "required": ["identity", "readiness"],
            "additionalProperties": False,
        },
    ]
}

_IDEMPOTENCY = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["safe", "idempotency-key", "never"],
        },
        "keyField": {"type": ["string", "null"]},
        "scope": {"type": ["string", "null"]},
    },
    "required": ["mode", "keyField", "scope"],
    "additionalProperties": False,
}

_STREAM = {
    "type": "object",
    "properties": {
        "cursorField": {"type": "string"},
        "maxBatch": {"type": "integer", "minimum": 1},
        "waitField": {"type": ["string", "null"]},
        "terminalStates": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["cursorField", "maxBatch", "waitField", "terminalStates"],
    "additionalProperties": False,
}

_PREVIEW = {
    "type": "object",
    "properties": {
        "operationField": {"type": "string"},
        "expiresField": {"type": "string"},
        "summaryField": {"type": "string"},
        "ttlSeconds": {"type": "integer"},
        "binds": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "operationField",
        "expiresField",
        "summaryField",
        "ttlSeconds",
        "binds",
    ],
    "additionalProperties": False,
}

_ERROR = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "description": {"type": "string"},
        "dataSchema": {"oneOf": [{"type": "null"}, {"type": "object"}]},
    },
    "required": ["code", "description", "dataSchema"],
    "additionalProperties": False,
}

_METHOD = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "owner": {"type": "string"},
        "declared": {"const": True},
        "httpExposed": {"type": "boolean"},
        "httpBinding": _HTTP_BINDING,
        "state": {
            "type": "string",
            "enum": ["available", "not-ready", "unsupported", "disabled"],
        },
        "callable": {"type": "boolean"},
        "reason": {"type": ["string", "null"]},
        "mode": {"type": "string", "enum": ["unary", "cursor-poll"]},
        "mutates": {"type": "boolean"},
        "retry": {
            "type": "string",
            "enum": ["safe", "idempotency-key", "never"],
        },
        "confirmation": {
            "type": "string",
            "enum": ["none", "preview", "execute"],
        },
        "audience": {"type": "array", "items": {"type": "string"}},
        "paramsSchema": {"type": "object"},
        "resultSchema": {"type": "object"},
        "errors": {"type": "array", "items": _ERROR},
        "stream": _STREAM,
        "idempotency": _IDEMPOTENCY,
        "preview": _PREVIEW,
        "previewOf": {"type": "string"},
    },
    "required": [
        "name",
        "owner",
        "declared",
        "httpExposed",
        "httpBinding",
        "state",
        "callable",
        "reason",
        "mode",
        "mutates",
        "retry",
        "confirmation",
        "audience",
        "paramsSchema",
        "resultSchema",
        "errors",
    ],
    "additionalProperties": False,
}

_RESULT = {
    "type": "object",
    "properties": {
        "schemaVersion": {"const": 1},
        "catalogVersion": {"const": "1"},
        "epoch": {"type": "string", "minLength": 1},
        "packageVersion": {"type": "string", "minLength": 1},
        "methods": {"type": "array", "items": _METHOD},
    },
    "required": [
        "schemaVersion",
        "catalogVersion",
        "epoch",
        "packageVersion",
        "methods",
    ],
    "additionalProperties": False,
}

DAEMON_CATALOG_SPEC = MethodSpec(
    name="daemon.catalog",
    owner="daemon.catalog",
    params_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    result_schema=_RESULT,
    errors=(MethodError(ipc_errors.DAEMON_ERROR),),
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("internal",),
    http_exposed=False,
    http_binding=None,
    confirmation="none",
)


def _method_state(
    app: Any,
    spec: MethodSpec,
    *,
    restore_done: Any,
    probes: Mapping[str, Any],
) -> tuple[str, bool, str | None]:
    """Return ``(state, callable, reason)`` in the declaration's fixed order."""

    if not spec.http_exposed:
        return "disabled", False, None
    restore_set = getattr(restore_done, "is_set", None)
    if restore_set is None or not restore_set():
        return "not-ready", False, "RESTORE_IN_PROGRESS"
    binding = spec.effective_http_binding
    if binding is None:
        return "unsupported", False, "HTTP_BINDING_MISSING"
    readiness = binding.readiness
    if readiness is not None and readiness not in probes:
        return "unsupported", False, "READINESS_PROBE_MISSING"
    if readiness is not None and not probes[readiness](app):
        return "not-ready", False, "SERVICE_NOT_READY"
    return "available", True, None


def daemon_catalog_handler(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    """Project declarations plus live state; no business read or invocation."""

    from ..registry import METHOD_REGISTRY

    specs = {spec.name: spec for spec in METHOD_REGISTRY.specs}
    entries = export_catalog(METHOD_REGISTRY.specs)["methods"]
    methods: list[JsonObject] = []
    for entry in entries:
        spec = specs[entry["name"]]
        state, callable_, reason = _method_state(
            app,
            spec,
            restore_done=getattr(app, "_restore_done", None),
            probes=READINESS_PROBES,
        )
        methods.append(
            {
                **entry,
                "declared": True,
                "state": state,
                "callable": callable_,
                "reason": reason,
            }
        )
    return {
        "schemaVersion": 1,
        "catalogVersion": "1",
        "epoch": app.epoch,
        "packageVersion": __version__,
        "methods": methods,
    }


CATALOG_BINDINGS = ((DAEMON_CATALOG_SPEC, daemon_catalog_handler),)

__all__ = ["CATALOG_BINDINGS", "DAEMON_CATALOG_SPEC", "daemon_catalog_handler"]
