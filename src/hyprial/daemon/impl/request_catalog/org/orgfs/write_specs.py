# Decision record (phase D, package D4b, 2026-11-25, design §1.1 / §1.2):
# ``orgfs.remove`` takes an optional boolean ``permanent``; ``orgfs.restore``
# takes no ``version`` (unknown keys are refused by the schema);
# ``orgfs.checkout`` declares ``ORGFS_LAYOUT_UNSUPPORTED`` and passes the
# refusal through with its data instead of wrapping it as ``checkout-io``.
"""Write ``orgfs.*`` MethodSpecs (batch 4).

Handlers forward to ``DaemonApplication._handle_orgfs``.  Export/import are
local-owner-selector methods because their path policy needs the trusted
local operator; the HTTP entry injects ``from`` and validates it against the
configured principal.
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

from ...address_errors import AGENT_ADDRESS_RETIRED_ERROR
from .wire import (
    MEMBER,
    NODE,
    LAYOUT_UNSUPPORTED_ERRORS,
    ORG_CONTEXT_ERRORS,
    PATH_ERRORS,
    PURGE_ERRORS,
    SESSION_ERRORS,
    SESSION_PROPERTIES,
    SPACE,
    String,
    WRITE_ERRORS,
)

JsonObject = dict[str, Any]

_SPACE_ID = {"type": "string", "minLength": 1}
_NODE_REF = {"type": "string", "minLength": 1}
_BOOL = {"type": "boolean"}
_HTTP_FORBIDDEN_FROM = {**_NODE_REF, "x-hyprial-http-forbidden": True}


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

    merged = {**SESSION_PROPERTIES, **properties}
    # ``actor`` is also an export/import business field; keep the transport
    # marker even when the caller's declaration overrides the property body.
    merged["actor"] = SESSION_PROPERTIES["actor"]
    merged["sessionRef"] = SESSION_PROPERTIES["sessionRef"]
    return _object(merged, required=required)


_OK = _object({"ok": {"const": True}}, required=("ok",))

_CREATE_PARAMS = _params({"name": _SPACE_ID}, required=("name",))

_WRITE_PARAMS = {
    "type": "object",
    "properties": {
        **SESSION_PROPERTIES,
        "spaceId": _SPACE_ID,
        "path": String,
        "node": String,
        "text": String,
        "contentB64": String,
        "baseVersion": String,
        "expectVersion": String,
        "createOnly": _BOOL,
    },
    "required": ["spaceId"],
    "additionalProperties": False,
    "oneOf": [
        {
            "type": "object",
            "required": ["text"],
            "properties": {"contentB64": {"type": "null"}},
        },
        {
            "type": "object",
            "required": ["contentB64"],
            "properties": {
                "text": {"type": "null"},
                # createOnly is implemented for text writes only.
                "createOnly": {"const": False},
            },
        },
    ],
}

_EXPORT_PARAMS = _params(
    {
        "spaceId": _SPACE_ID,
        "path": String,
        "node": String,
        "destination": _NODE_REF,
        "overwrite": _BOOL,
        "waitSeconds": {"type": "number", "minimum": 0, "maximum": 120},
        "from": _HTTP_FORBIDDEN_FROM,
        "actor": String,
    },
    required=("spaceId", "destination", "from"),
)

_IMPORT_PARAMS = _params(
    {
        "spaceId": _SPACE_ID,
        "path": String,
        "node": String,
        "source": _NODE_REF,
        "from": _HTTP_FORBIDDEN_FROM,
        "actor": String,
    },
    required=("spaceId", "source", "from"),
)

_MKDIR_PARAMS = _params(
    {"spaceId": _SPACE_ID, "path": _NODE_REF},
    required=("spaceId", "path"),
)

_MOVE_PARAMS = {
    "type": "object",
    "properties": {
        **SESSION_PROPERTIES,
        "spaceId": _SPACE_ID,
        "sourcePath": _NODE_REF,
        "source": _NODE_REF,
        "destinationPath": _NODE_REF,
        "destination": _NODE_REF,
    },
    "required": ["spaceId"],
    "additionalProperties": False,
    "allOf": [
        {
            "type": "object",
            "anyOf": [
                {"type": "object", "required": ["sourcePath"]},
                {"type": "object", "required": ["source"]},
            ],
        },
        {
            "type": "object",
            "anyOf": [
                {"type": "object", "required": ["destinationPath"]},
                {"type": "object", "required": ["destination"]},
            ],
        },
    ],
}

_REMOVE_PARAMS = _params(
    {"spaceId": _SPACE_ID, "path": String, "node": String, "permanent": _BOOL},
    required=("spaceId",),
)

_RESTORE_PARAMS = _params(
    {
        "spaceId": _SPACE_ID,
        "node": _NODE_REF,
        "recursive": _BOOL,
    },
    required=("spaceId", "node"),
)

_SPACE_PARAMS = _params({"spaceId": _SPACE_ID}, required=("spaceId",))

_CHECKOUT_PARAMS = _params(
    {"spaceId": _SPACE_ID, "enabled": _BOOL},
    required=("spaceId", "enabled"),
)

_INVITE_PARAMS = _params(
    {
        "spaceId": _SPACE_ID,
        "user": _NODE_REF,
        "mode": {"type": "string", "enum": ["ro", "rw"]},
    },
    required=("spaceId", "user"),
)

_REMOVE_MEMBER_PARAMS = _params(
    {"spaceId": _SPACE_ID, "user": _NODE_REF},
    required=("spaceId", "user"),
)

_JOIN_PARAMS = _params({"spaceId": _SPACE_ID}, required=("spaceId",))

_SPACE_PURGE_RESULT = _object(
    {"spaceId": String, "purged": _BOOL, "localOnly": _BOOL},
    required=("spaceId", "purged", "localOnly"),
)

_CHECKOUT_RESULT = _object(
    {"spaceId": String, "enabled": _BOOL, "path": String},
    required=("spaceId", "enabled", "path"),
)


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


def _handler(method: str):  # noqa: ANN202 - small generated-style wrapper
    def call(
        app: Any,
        params: Mapping[str, Any],
        *,
        trusted_message_origin: str | None = None,
    ) -> JsonObject:
        return _forward(method, app, params, trusted_message_origin)

    call.__name__ = method.replace(".", "_")
    return call


def _write_spec(
    name: str,
    params_schema: JsonObject,
    result_schema: JsonObject,
    *,
    errors: tuple[Any, ...] = WRITE_ERRORS,
    identity: IdentityBinding | None = None,
) -> MethodSpec:
    return MethodSpec(
        name=name,
        owner="daemon.orgfs",
        params_schema=params_schema,
        result_schema=result_schema,
        errors=errors,
        mode="unary",
        mutates=True,
        retry="never",
        audience=("local-operator",),
        http_exposed=True,
        http_binding=HttpBinding(identity or IdentityBinding("none")),
        confirmation="none",
        idempotency=IdempotencySpec(mode="never", scope=name),
    )


_SPACE_WRITE_ERRORS = WRITE_ERRORS + LAYOUT_UNSUPPORTED_ERRORS

ORGFS_CREATE_SPEC = _write_spec("orgfs.create", _CREATE_PARAMS, SPACE)
ORGFS_WRITE_SPEC = _write_spec("orgfs.write", _WRITE_PARAMS, NODE, errors=_SPACE_WRITE_ERRORS)
ORGFS_EXPORT_SPEC = _write_spec(
    "orgfs.export",
    _EXPORT_PARAMS,
    NODE,
    # The path policy authenticates ``from`` (address admission).
    errors=PATH_ERRORS + ORG_CONTEXT_ERRORS + LAYOUT_UNSUPPORTED_ERRORS + (AGENT_ADDRESS_RETIRED_ERROR,),
    identity=IdentityBinding("local-owner-selector", "from"),
)
ORGFS_IMPORT_SPEC = _write_spec(
    "orgfs.import",
    _IMPORT_PARAMS,
    NODE,
    errors=PATH_ERRORS + SESSION_ERRORS + LAYOUT_UNSUPPORTED_ERRORS + (AGENT_ADDRESS_RETIRED_ERROR,),
    identity=IdentityBinding("local-owner-selector", "from"),
)
ORGFS_MKDIR_SPEC = _write_spec("orgfs.mkdir", _MKDIR_PARAMS, NODE, errors=_SPACE_WRITE_ERRORS)
ORGFS_MOVE_SPEC = _write_spec("orgfs.move", _MOVE_PARAMS, NODE, errors=_SPACE_WRITE_ERRORS)
ORGFS_REMOVE_SPEC = _write_spec("orgfs.remove", _REMOVE_PARAMS, _OK, errors=_SPACE_WRITE_ERRORS)
ORGFS_RESTORE_SPEC = _write_spec("orgfs.restore", _RESTORE_PARAMS, NODE, errors=_SPACE_WRITE_ERRORS)
ORGFS_SPACE_PURGE_SPEC = _write_spec(
    "orgfs.space_purge", _SPACE_PARAMS, _SPACE_PURGE_RESULT, errors=PURGE_ERRORS
)
ORGFS_CHECKOUT_SPEC = _write_spec(
    "orgfs.checkout", _CHECKOUT_PARAMS, _CHECKOUT_RESULT, errors=_SPACE_WRITE_ERRORS
)
ORGFS_INVITE_SPEC = _write_spec("orgfs.invite", _INVITE_PARAMS, MEMBER, errors=_SPACE_WRITE_ERRORS)
ORGFS_REMOVE_MEMBER_SPEC = _write_spec(
    "orgfs.remove_member", _REMOVE_MEMBER_PARAMS, _OK, errors=_SPACE_WRITE_ERRORS
)
ORGFS_JOIN_SPEC = _write_spec("orgfs.join", _JOIN_PARAMS, SPACE)

BATCH4_ORGFS_BINDINGS = (
    (ORGFS_CREATE_SPEC, _handler("orgfs.create")),
    (ORGFS_WRITE_SPEC, _handler("orgfs.write")),
    (ORGFS_EXPORT_SPEC, _handler("orgfs.export")),
    (ORGFS_IMPORT_SPEC, _handler("orgfs.import")),
    (ORGFS_MKDIR_SPEC, _handler("orgfs.mkdir")),
    (ORGFS_MOVE_SPEC, _handler("orgfs.move")),
    (ORGFS_REMOVE_SPEC, _handler("orgfs.remove")),
    (ORGFS_RESTORE_SPEC, _handler("orgfs.restore")),
    (ORGFS_SPACE_PURGE_SPEC, _handler("orgfs.space_purge")),
    (ORGFS_CHECKOUT_SPEC, _handler("orgfs.checkout")),
    (ORGFS_INVITE_SPEC, _handler("orgfs.invite")),
    (ORGFS_REMOVE_MEMBER_SPEC, _handler("orgfs.remove_member")),
    (ORGFS_JOIN_SPEC, _handler("orgfs.join")),
)

__all__ = [
    "BATCH4_ORGFS_BINDINGS",
    "ORGFS_CREATE_SPEC",
    "ORGFS_WRITE_SPEC",
    "ORGFS_EXPORT_SPEC",
    "ORGFS_IMPORT_SPEC",
    "ORGFS_MKDIR_SPEC",
    "ORGFS_MOVE_SPEC",
    "ORGFS_REMOVE_SPEC",
    "ORGFS_RESTORE_SPEC",
    "ORGFS_SPACE_PURGE_SPEC",
    "ORGFS_CHECKOUT_SPEC",
    "ORGFS_INVITE_SPEC",
    "ORGFS_REMOVE_MEMBER_SPEC",
    "ORGFS_JOIN_SPEC",
]
