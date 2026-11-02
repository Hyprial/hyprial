"""The migrated ``link.*`` methods: declarations plus real service handlers."""

from __future__ import annotations

import time
from collections.abc import Mapping
from functools import partial
from typing import Any

from hyprial.daemon.impl.ipc.params import _required_string
from hyprial.daemon.impl.links.ports import (
    IPC_LINK_ADD,
    IPC_LINK_DROP,
    IPC_LINK_LIST,
    IPC_LINK_STATS,
)
from hyprial.kernel import (
    DaemonRequestError,
    HttpBinding,
    IdentityBinding,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    ipc_errors,
    parse_agent_uri,
)

JsonObject = dict[str, Any]

_FROM = {
    "type": "object",
    "properties": {
        "from": {"type": "string", "minLength": 1},
    },
    "required": ["from"],
    "additionalProperties": False,
}

_LINK_ADMIN_PARAMS = {
    "type": "object",
    "properties": {
        "from": {"type": "string", "minLength": 1},
        "peer": {"type": "string", "minLength": 1},
        "sessionRef": {"type": "string", "minLength": 1},
        "onBehalfOf": {"type": "string", "minLength": 1},
    },
    "required": ["from", "peer"],
    "additionalProperties": False,
}

_OWNER_MAP = {
    "type": "object",
    "additionalProperties": {"type": "string"},
}

_LINK_ROW = {
    "type": "object",
    "properties": {
        "peer": {"type": "string", "minLength": 1},
        "state": {"type": "string", "minLength": 1},
        "cwnd": {"type": ["integer", "null"], "minimum": 0},
        "inFlight": {"type": "integer", "minimum": 0},
        "lifecycle": {"type": ["string", "null"]},
        "taskId": {"type": ["string", "null"]},
        "taskIds": {"type": "array", "items": {"type": "string"}},
        "mintedAtMs": {"type": ["integer", "null"]},
        "lastUsedAtMs": {"type": ["integer", "null"]},
        "revision": {"type": ["integer", "null"], "minimum": 0},
        "mine": {"type": "boolean"},
        "theirs": {"type": "boolean"},
        "committed": {"type": "boolean"},
    },
    "required": ["peer", "state", "inFlight"],
    "additionalProperties": False,
}

_HELD_ROW = {
    "type": "object",
    "properties": {
        "messageId": {"type": "string", "minLength": 1},
        "target": {"type": "string", "minLength": 1},
        "holdReason": {"type": "string", "minLength": 1},
        "detail": {"type": "string"},
        "heldMs": {"type": "integer", "minimum": 0},
    },
    "required": ["messageId", "target", "holdReason", "detail", "heldMs"],
    "additionalProperties": False,
}

_LINK_LIST_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "actor": {"type": "string", "minLength": 1},
        "orgAvailable": {"type": "boolean"},
        "bypass": {"type": "boolean"},
        "bookkeeping": {"type": "boolean"},
        "k": {"type": "integer", "minimum": 0},
        "W": {"type": "integer", "minimum": 0},
        "inFlight": {"type": "integer", "minimum": 0},
        "owner": _OWNER_MAP,
        "links": {"type": "array", "items": _LINK_ROW},
        "held": {"type": "array", "items": _HELD_ROW},
    },
    "required": ["ok", "actor", "links"],
    "additionalProperties": False,
}

_STATS_ROW = {
    "type": "object",
    "properties": {
        "peer": {"type": "string", "minLength": 1},
        "rttMinMs": {"type": ["number", "null"]},
        "rttMaxMs": {"type": ["number", "null"]},
        "rttMs": {"type": ["number", "null"]},
        "cwnd": {"type": ["integer", "null"], "minimum": 0},
        "inFlight": {"type": "integer", "minimum": 0},
    },
    "required": ["peer", "inFlight"],
    "additionalProperties": False,
}

_LINK_STATS_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "actor": {"type": "string", "minLength": 1},
        "nActive": {"type": "integer", "minimum": 0},
        "k": {"type": "integer", "minimum": 0},
        "W": {"type": "integer", "minimum": 0},
        "windowObserved": {"type": "integer", "minimum": 0},
        "bypass": {"type": "boolean"},
        "wTarget": {"type": ["integer", "null"]},
        "tMinMs": {"type": ["integer", "null"]},
        "tMaxMs": {"type": ["integer", "null"]},
        "tBarMs": {"type": ["integer", "null"]},
        "muMax": {"type": ["number", "null"]},
        "diff": {"type": ["number", "null"]},
        "grad": {"type": ["number", "null"]},
        "periodMs": {"type": ["integer", "null"]},
        "links": {"type": "array", "items": _STATS_ROW},
    },
    "required": ["ok", "actor", "nActive", "links"],
    "additionalProperties": False,
}

_LINK_CHANGE_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "actor": {"type": "string", "minLength": 1},
        "peer": {"type": "string", "minLength": 1},
        "state": {"type": "string", "minLength": 1},
        "revision": {"type": ["integer", "null"], "minimum": 0},
        "committed": {"type": "boolean"},
        "written": {"type": "boolean"},
        "queued": {"type": "boolean"},
        "reason": {"type": "string"},
        "holdReason": {"type": "string"},
        "detail": {"type": "string"},
    },
    "required": ["ok", "actor", "peer", "state"],
    "additionalProperties": False,
}

_INVALID_ARGUMENT_DATA = {
    "type": "object",
    "properties": {
        "actor": {"type": "string"},
        "peer": {"type": "string"},
    },
    "additionalProperties": False,
}

_CLAIMED_IDENTITY_DATA = {
    "type": "object",
    "properties": {"claimedIdentity": {"type": "string"}},
    "additionalProperties": False,
}

_SENDER_DATA = {
    "type": "object",
    "properties": {"sender": {"type": "string"}},
    "additionalProperties": False,
}

_QUERY_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT, _INVALID_ARGUMENT_DATA),
    MethodError(ipc_errors.DAEMON_NOT_READY, description="link service is not wired"),
)

_ADMIN_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT, _INVALID_ARGUMENT_DATA),
    MethodError(ipc_errors.DAEMON_NOT_READY, description="link service is not wired"),
    MethodError(ipc_errors.CALLER_NOT_AUTHORIZED, _CLAIMED_IDENTITY_DATA),
    MethodError(ipc_errors.SENDER_UNRESOLVED, _SENDER_DATA),
    MethodError(ipc_errors.SENDER_NOT_AUTHENTICATED, _CLAIMED_IDENTITY_DATA),
    MethodError(ipc_errors.STALE_SESSION),
    MethodError(ipc_errors.SESSION_SUPERSEDED),
    MethodError(ipc_errors.INVALID_SESSION_SOURCE),
)

LINK_LIST_SPEC = MethodSpec(
    name=IPC_LINK_LIST,
    owner="daemon.links",
    params_schema=_FROM,
    result_schema=_LINK_LIST_RESULT,
    errors=_QUERY_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator", "bound-agent"),
    http_exposed=True,
    http_binding=HttpBinding(
        IdentityBinding("local-operator-selector", "from"),
        readiness="link-service",
    ),
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)

LINK_STATS_SPEC = MethodSpec(
    name=IPC_LINK_STATS,
    owner="daemon.links",
    params_schema=_FROM,
    result_schema=_LINK_STATS_RESULT,
    errors=_QUERY_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator", "bound-agent"),
    http_exposed=True,
    http_binding=HttpBinding(
        IdentityBinding("local-operator-selector", "from"),
        readiness="link-service",
    ),
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)

LINK_ADD_SPEC = MethodSpec(
    name=IPC_LINK_ADD,
    owner="daemon.links",
    params_schema=_LINK_ADMIN_PARAMS,
    result_schema=_LINK_CHANGE_RESULT,
    errors=_ADMIN_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator", "bound-agent"),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="one-sided-link-reference"),
)

LINK_DROP_SPEC = MethodSpec(
    name=IPC_LINK_DROP,
    owner="daemon.links",
    params_schema=_LINK_ADMIN_PARAMS,
    result_schema=_LINK_CHANGE_RESULT,
    errors=_ADMIN_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator", "bound-agent"),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="link-reclaim"),
)


def _service(app: Any) -> Any:
    service = getattr(app, "_link_service", None)
    if service is None:
        raise DaemonRequestError(
            ipc_errors.DAEMON_NOT_READY, "link service is not wired"
        )
    return service


def _actor(app: Any, params: Mapping[str, Any]) -> str:
    return app._canonical_interactive_actor(_required_string(params.get("from"), "from"))


def _link_actor(app: Any, subject: str) -> str:
    """Refuse a link-file owner that is not this node's canonical agent."""

    parsed = parse_agent_uri(subject)
    if parsed is None or parsed[0] != app.owner or parsed[1] != app.node_id:
        raise DaemonRequestError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            "link administration requires a canonical agent identity of this node",
            {"claimedIdentity": subject},
        )
    return subject


def link_list(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    service = _service(app)
    return {"ok": True, **service.list_links(_actor(app, params))}


def link_stats(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    service = _service(app)
    return {"ok": True, **service.stats(_actor(app, params))}


def _admin_actor(
    app: Any,
    params: Mapping[str, Any],
    method: str,
    *,
    trusted_message_origin: str | None,
) -> str:
    caller = app._authenticated_message_caller(
        dict(params), method=method, trusted_origin=trusted_message_origin
    )
    return _link_actor(app, caller.subject)


def link_add(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    service = _service(app)
    actor = _admin_actor(
        app, params, IPC_LINK_ADD, trusted_message_origin=trusted_message_origin
    )
    peer = _required_string(params.get("peer"), "peer")
    return {
        "ok": True,
        **service.add(actor=actor, peer=peer, now_ms=int(time.time() * 1000)),
    }


def link_drop(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    service = _service(app)
    actor = _admin_actor(
        app, params, IPC_LINK_DROP, trusted_message_origin=trusted_message_origin
    )
    peer = _required_string(params.get("peer"), "peer")
    return {
        "ok": True,
        **service.drop(actor=actor, peer=peer, now_ms=int(time.time() * 1000)),
    }


def _link_param_error(
    method: str, error: Any, params: Mapping[str, Any]
) -> str:
    """Presentation-only mapping of schema violations to the legacy messages.

    The registry still runs the full schema validator first; this function only
    chooses the error text and never calls a handler or a service.
    """

    validator = getattr(error, "validator", None)
    if validator == "required":
        missing = [
            name
            for name in getattr(error, "validator_value", ())
            if name not in params
        ]
        field = missing[0] if missing else ""
    else:
        path = list(getattr(error, "absolute_path", ()))
        field = str(path[-1]) if path else ""
    if field in {"from", "peer"} and validator in {"required", "type", "minLength"}:
        return f"{field} must be a non-empty string"
    path_text = "/".join(
        str(part) for part in getattr(error, "absolute_path", ())
    ) or "<root>"
    return f"{method} params do not match the declared schema at {path_text}"


LINK_BINDINGS = (
    (LINK_LIST_SPEC, link_list, partial(_link_param_error, IPC_LINK_LIST)),
    (LINK_STATS_SPEC, link_stats, partial(_link_param_error, IPC_LINK_STATS)),
    (LINK_ADD_SPEC, link_add, partial(_link_param_error, IPC_LINK_ADD)),
    (LINK_DROP_SPEC, link_drop, partial(_link_param_error, IPC_LINK_DROP)),
)

__all__ = [
    "LINK_ADD_SPEC",
    "LINK_BINDINGS",
    "LINK_DROP_SPEC",
    "LINK_LIST_SPEC",
    "LINK_STATS_SPEC",
    "link_add",
    "link_drop",
    "link_list",
    "link_stats",
]
