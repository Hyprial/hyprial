"""Read-only ``orgfs.*`` MethodSpecs (batch 3).

Every handler forwards to ``DaemonApplication._handle_orgfs``; no orgfs store
logic is copied.  Read methods are ``mutates=False`` / ``retry="safe"`` and
keep the local-operator audience.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.kernel import (
    DaemonRequestError,
    HttpBinding,
    IdentityBinding,
    IdempotencySpec,
    MethodSpec,
    ipc_errors,
)

from .wire import (
    Boolean,
    CHANGE_EVENT,
    HISTORY_ENTRY,
    MEMBER,
    NODE,
    ORG_CONTEXT_ERRORS,
    PURGE_STATUS,
    PURGE_STATUS_ERRORS,
    READ_ERRORS,
    SESSION_PROPERTIES,
    SPACE,
    SPACE_STATUS,
    String,
)

JsonObject = dict[str, Any]

_SPACE_ID = {"type": "string", "minLength": 1}
_NODE_REF = {"type": "string", "minLength": 1}
_LIMIT = {"type": "integer", "minimum": 1}


def _object(
    properties: Mapping[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> JsonObject:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def _params(
    properties: Mapping[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> JsonObject:
    """One params object plus the MCP session identity the proxy always adds."""

    return _object({**SESSION_PROPERTIES, **properties}, required=required)


def _nodes_result() -> JsonObject:
    return _object({"nodes": {"type": "array", "items": NODE}})


_SPACES_PARAMS = _params({})

_RESOLVE_PARAMS = _params(
    {"spaceId": _SPACE_ID, "path": _NODE_REF},
    required=("spaceId", "path"),
)

_LS_PARAMS = _params(
    {
        "spaceId": _SPACE_ID,
        "path": String,
        "node": String,
        "recursive": Boolean,
    },
    required=("spaceId",),
)

_STAT_PARAMS = _params({"spaceId": _SPACE_ID, "path": String, "node": String}, required=("spaceId",))

_READ_PARAMS = _params(
    {
        "spaceId": _SPACE_ID,
        "path": String,
        "node": String,
        "waitSeconds": {"type": "number", "minimum": 0, "maximum": 120},
    },
    required=("spaceId",),
)

_READ_AT_PARAMS = _params(
    {"spaceId": _SPACE_ID, "node": _NODE_REF, "version": _NODE_REF},
    required=("spaceId", "node", "version"),
)

_STAT_AT_PARAMS = dict(_READ_AT_PARAMS)

_HISTORY_PARAMS = _params(
    {
        "spaceId": _SPACE_ID,
        "path": String,
        "node": String,
        "limit": _LIMIT,
        "before": String,
    },
    required=("spaceId",),
)

_STATUS_PARAMS = _params({"spaceId": _SPACE_ID}, required=("spaceId",))

_TRASH_PARAMS = _params({"spaceId": _SPACE_ID, "limit": _LIMIT}, required=("spaceId",))

_WATCH_PARAMS = _params(
    {"spaceId": _SPACE_ID, "glob": String, "sinceVersion": String},
    required=("spaceId",),
)

_MEMBERS_PARAMS = _params({"spaceId": _SPACE_ID}, required=("spaceId",))

_PURGE_STATUS_PARAMS = _params(
    {"spaceId": _SPACE_ID, "planId": _NODE_REF},
    required=("spaceId", "planId"),
)

_READ_RESULT = {
    "type": "object",
    "properties": {
        "node": NODE,
        "text": String,
        "contentB64": String,
        "version": String,
    },
    "required": ["node"],
    "additionalProperties": False,
    "oneOf": [
        {"type": "object", "required": ["text", "version"]},
        {"type": "object", "required": ["contentB64"]},
    ],
}

_HISTORY_RESULT = _object({"history": {"type": "array", "items": HISTORY_ENTRY}})
_WATCH_RESULT = _object({"events": {"type": "array", "items": CHANGE_EVENT}})
_MEMBERS_RESULT = _object({"members": {"type": "array", "items": MEMBER}})
_SPACES_RESULT = _object({"spaces": {"type": "array", "items": SPACE}})


def _forward(
    method: str,
    app: Any,
    params: Mapping[str, Any],
    trusted_message_origin: str | None,
) -> JsonObject:
    from hyprial.daemon.impl.orgfs.api import OrgFsError

    try:
        return app._handle_orgfs(
            method, dict(params), trusted_origin=trusted_message_origin
        )
    except (DaemonRequestError, OrgFsError) as error:
        # The orgfs facade and the shared IPC layer both spell argument
        # failures ``invalid-argument``/``INVALID_ARGUMENT``.  The contract
        # exposes the shared code exactly once.
        if error.code == "invalid-argument":
            data = (
                error.data
                if isinstance(error, DaemonRequestError)
                else error.details
            )
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, str(error), data=data
            ) from error
        raise


def orgfs_spaces(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return _forward("orgfs.spaces", app, params, trusted_message_origin)


def orgfs_resolve(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return _forward("orgfs.resolve", app, params, trusted_message_origin)


def orgfs_ls(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return _forward("orgfs.ls", app, params, trusted_message_origin)


def orgfs_stat(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return _forward("orgfs.stat", app, params, trusted_message_origin)


def orgfs_read(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return _forward("orgfs.read", app, params, trusted_message_origin)


def orgfs_read_at(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return _forward("orgfs.read_at", app, params, trusted_message_origin)


def orgfs_stat_at(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return _forward("orgfs.stat_at", app, params, trusted_message_origin)


def orgfs_history(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return _forward("orgfs.history", app, params, trusted_message_origin)


def orgfs_status(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return _forward("orgfs.status", app, params, trusted_message_origin)


def orgfs_trash(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return _forward("orgfs.trash", app, params, trusted_message_origin)


def orgfs_watch(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return _forward("orgfs.watch", app, params, trusted_message_origin)


def orgfs_members(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return _forward("orgfs.members", app, params, trusted_message_origin)


def orgfs_purge_status(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return _forward("orgfs.purge_status", app, params, trusted_message_origin)


def _spec(
    name: str,
    params_schema: JsonObject,
    result_schema: JsonObject,
    errors: tuple[Any, ...] = READ_ERRORS,
) -> MethodSpec:
    return MethodSpec(
        name=name,
        owner="daemon.orgfs",
        params_schema=params_schema,
        result_schema=result_schema,
        errors=errors,
        mode="unary",
        mutates=False,
        retry="safe",
        audience=("local-operator",),
        http_exposed=True,
        http_binding=HttpBinding(IdentityBinding("none")),
        confirmation="none",
        idempotency=IdempotencySpec(mode="safe", scope="orgfs-read"),
    )


#: The application enforces org-context on exactly these reads.  The session
#: fence can also answer STALE_SESSION / SESSION_SUPERSEDED before the grant
#: check, so those two are declared here rather than on every orgfs method.
_ORG_CONTEXT_READ_ERRORS = READ_ERRORS + ORG_CONTEXT_ERRORS

ORGFS_SPACES_SPEC = _spec("orgfs.spaces", _SPACES_PARAMS, _SPACES_RESULT)
ORGFS_RESOLVE_SPEC = _spec("orgfs.resolve", _RESOLVE_PARAMS, _nodes_result())
ORGFS_LS_SPEC = _spec(
    "orgfs.ls", _LS_PARAMS, _nodes_result(), _ORG_CONTEXT_READ_ERRORS
)
ORGFS_STAT_SPEC = _spec(
    "orgfs.stat", _STAT_PARAMS, NODE, _ORG_CONTEXT_READ_ERRORS
)
ORGFS_READ_SPEC = _spec(
    "orgfs.read", _READ_PARAMS, _READ_RESULT, _ORG_CONTEXT_READ_ERRORS
)
ORGFS_READ_AT_SPEC = _spec(
    "orgfs.read_at",
    _READ_AT_PARAMS,
    _object({"contentB64": String}, required=("contentB64",)),
    _ORG_CONTEXT_READ_ERRORS,
)
ORGFS_STAT_AT_SPEC = _spec(
    "orgfs.stat_at", _STAT_AT_PARAMS, NODE, _ORG_CONTEXT_READ_ERRORS
)
ORGFS_HISTORY_SPEC = _spec(
    "orgfs.history", _HISTORY_PARAMS, _HISTORY_RESULT, _ORG_CONTEXT_READ_ERRORS
)
ORGFS_STATUS_SPEC = _spec("orgfs.status", _STATUS_PARAMS, SPACE_STATUS)
ORGFS_TRASH_SPEC = _spec("orgfs.trash", _TRASH_PARAMS, _nodes_result())
ORGFS_WATCH_SPEC = _spec("orgfs.watch", _WATCH_PARAMS, _WATCH_RESULT)
ORGFS_MEMBERS_SPEC = _spec("orgfs.members", _MEMBERS_PARAMS, _MEMBERS_RESULT)
ORGFS_PURGE_STATUS_SPEC = _spec(
    "orgfs.purge_status",
    _PURGE_STATUS_PARAMS,
    PURGE_STATUS,
    PURGE_STATUS_ERRORS,
)

BATCH3_ORGFS_BINDINGS = (
    (ORGFS_SPACES_SPEC, orgfs_spaces),
    (ORGFS_RESOLVE_SPEC, orgfs_resolve),
    (ORGFS_LS_SPEC, orgfs_ls),
    (ORGFS_STAT_SPEC, orgfs_stat),
    (ORGFS_READ_SPEC, orgfs_read),
    (ORGFS_READ_AT_SPEC, orgfs_read_at),
    (ORGFS_STAT_AT_SPEC, orgfs_stat_at),
    (ORGFS_HISTORY_SPEC, orgfs_history),
    (ORGFS_STATUS_SPEC, orgfs_status),
    (ORGFS_TRASH_SPEC, orgfs_trash),
    (ORGFS_WATCH_SPEC, orgfs_watch),
    (ORGFS_MEMBERS_SPEC, orgfs_members),
    (ORGFS_PURGE_STATUS_SPEC, orgfs_purge_status),
)
