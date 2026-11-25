"""Batch 5: ``hosts`` and ``top.snapshot`` MethodSpecs.

Both are read-only daemon projections.  Their nested row shapes are owned by
the status projection and may grow; the contract freezes the top-level
envelope and the host row fields the GUI consumes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.kernel import (
    DaemonRequestError,
    HttpBinding,
    IdentityBinding,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    ipc_errors,
)

JsonObject = dict[str, Any]

_ANY_VALUE = {
    "type": ["string", "number", "boolean", "null", "array", "object"],
}

_HOST_ROW = {
    "type": "object",
    "properties": {
        "nodeId": {"type": "string", "minLength": 1},
        "status": {"type": "string", "enum": ["online", "unknown"]},
        "observedAtMs": {"type": ["integer", "null"]},
        "ageMs": {"type": ["integer", "null"]},
        "stale": {"type": "boolean"},
        "lastError": {"type": ["string", "null"]},
    },
    "required": ["nodeId", "status", "observedAtMs", "ageMs", "stale", "lastError"],
    "additionalProperties": False,
}

_REFRESH = {
    "type": "object",
    "properties": {
        "accepted": {"type": "boolean"},
        "queued": {"type": "boolean"},
        "operationId": {"type": ["string", "null"]},
        "coalesced": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["accepted", "queued", "operationId", "coalesced"],
    "additionalProperties": False,
}

_HOSTS_RESULT = {
    "type": "object",
    "properties": {
        "hosts": {"type": "array", "items": _HOST_ROW},
        "refresh": _REFRESH,
    },
    "required": ["hosts"],
    "additionalProperties": False,
}

#: The top snapshot is a diagnostic projection; its daemon block carries
#: runtime identity/clock fields that the committed OpenAPI must not freeze.
#: The contract keeps the envelope and lets the row fields evolve.
_TOP_DAEMON = {
    "type": "object",
    "properties": {},
    "additionalProperties": _ANY_VALUE,
}

_TOP_ROW = {
    "type": "object",
    "properties": {},
    "additionalProperties": _ANY_VALUE,
}

_TOP_RESULT = {
    "type": "object",
    "properties": {
        "daemon": _TOP_DAEMON,
        "actors": {"type": "array", "items": _TOP_ROW},
        "stranded": {"type": "array", "items": _TOP_ROW},
        "quota": _TOP_ROW,
    },
    "required": ["daemon", "actors", "stranded", "quota"],
    "additionalProperties": _ANY_VALUE,
}

_HOSTS_PARAMS = {
    "type": "object",
    "properties": {
        "refresh": {"type": "boolean"},
        "refreshTailnetStatus": {"type": "boolean"},
    },
    "additionalProperties": False,
}

_TOP_PARAMS = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT, description="no data"),
    MethodError(ipc_errors.DAEMON_NOT_READY, description="the status service is not wired"),
)


def hosts(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    handler = getattr(app, "_ipc_hosts", None)
    if handler is None:
        raise DaemonRequestError(
            ipc_errors.DAEMON_NOT_READY, "hosts is not wired"
        )
    return handler(dict(params))


def top_snapshot(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    handler = getattr(app, "_ipc_top_snapshot", None)
    if handler is None:
        raise DaemonRequestError(
            ipc_errors.DAEMON_NOT_READY, "top.snapshot is not wired"
        )
    return handler()


HOSTS_SPEC = MethodSpec(
    name="hosts",
    owner="daemon.status",
    params_schema=_HOSTS_PARAMS,
    result_schema=_HOSTS_RESULT,
    errors=_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator",),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)

TOP_SNAPSHOT_SPEC = MethodSpec(
    name="top.snapshot",
    owner="daemon.status",
    params_schema=_TOP_PARAMS,
    result_schema=_TOP_RESULT,
    errors=_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator",),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)

_TOP_METRICS = {
    "type": "object",
    "properties": {"cpuPercent": {"type": "number"}, "rssKb": {"type": "integer"},
                   "cpuSeconds": {"type": "number"}, "elapsedSeconds": {"type": "number"}},
    "required": ["cpuPercent", "rssKb", "cpuSeconds", "elapsedSeconds"],
    "additionalProperties": False,
}
_TOP_INSPECT_ROW = {"type": "object",
    "properties": {"processMetrics": {"oneOf": [_TOP_METRICS, {"type": "null"}]}},
    "required": ["processMetrics"], "additionalProperties": _ANY_VALUE}
_TOP_INSPECT_RESULT = {**_TOP_RESULT, "properties": {**_TOP_RESULT["properties"],
    "actors": {"type": "array", "items": _TOP_INSPECT_ROW}}}


def top_inspect(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> JsonObject:
    from hyprial.daemon.impl.operations.top import inspect_snapshot

    return inspect_snapshot(top_snapshot(app, params, trusted_message_origin=trusted_message_origin))


TOP_INSPECT_SPEC = MethodSpec(
    name="top.inspect", owner="daemon.status", params_schema=_TOP_PARAMS,
    result_schema=_TOP_INSPECT_RESULT, errors=(*_ERRORS, MethodError(ipc_errors.DAEMON_RESTORING),
                                               MethodError(ipc_errors.DAEMON_ERROR)), mode="unary", mutates=False,
    retry="safe", audience=("local-operator",), http_exposed=False, confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="top-inspect"),
)


BATCH5_VIEW_BINDINGS = (
    (HOSTS_SPEC, hosts),
    (TOP_SNAPSHOT_SPEC, top_snapshot),
    (TOP_INSPECT_SPEC, top_inspect),
)

__all__ = [
    "BATCH5_VIEW_BINDINGS",
    "HOSTS_SPEC",
    "TOP_SNAPSHOT_SPEC",
    "hosts",
    "top_snapshot",
]
