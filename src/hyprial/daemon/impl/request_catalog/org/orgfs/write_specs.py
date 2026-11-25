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
_SHA = {"type": "string", "minLength": 64, "maxLength": 64}
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
    {"spaceId": _SPACE_ID, "path": String, "node": String},
    required=("spaceId",),
)

_RESTORE_PARAMS = _params(
    {
        "spaceId": _SPACE_ID,
        "node": _NODE_REF,
        "version": _NODE_REF,
        "recursive": _BOOL,
    },
    required=("spaceId", "node", "version"),
)

_PURGE_TARGET = {
    "oneOf": [
        _object({"kind": {"const": "blob"}, "sha": _SHA}, required=("kind", "sha")),
        _object(
            {"kind": {"const": "tree-range"}, "from": String, "to": String},
            required=("kind", "from", "to"),
        ),
        _object(
            {
                "kind": {"const": "doc-range"},
                "node": _NODE_REF,
                "from": String,
                "to": String,
            },
            required=("kind", "node", "from", "to"),
        ),
    ]
}

_PURGE_PLAN_PARAMS = _params(
    {
        "spaceId": _SPACE_ID,
        "targets": {"type": "array", "items": _PURGE_TARGET, "minItems": 1},
    },
    required=("spaceId", "targets"),
)

_PURGE_PARAMS = _params(
    {"spaceId": _SPACE_ID, "planId": _NODE_REF},
    required=("spaceId", "planId"),
)

_SPACE_PARAMS = _params({"spaceId": _SPACE_ID}, required=("spaceId",))

_UNBAN_PARAMS = _params(
    {"spaceId": _SPACE_ID, "sha": _SHA}, required=("spaceId", "sha")
)

_SERVE_PARAMS = _params(
    {
        "spaceId": _SPACE_ID,
        "backend": {"type": "string", "enum": ["fs", "memory"]},
    },
    required=("spaceId",),
)

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

_PURGE_DOCUMENT = _object(
    {
        "doc_id": String,
        "start": String,
        "end": String,
        "update_blobs": {"type": "array", "items": String},
        "log_keys": {"type": "array", "items": String},
    },
    required=("doc_id", "start", "end", "update_blobs", "log_keys"),
)

_PURGE_BLOB = _object(
    {
        "sha": String,
        "referenced_elsewhere_locally": {"type": "array", "items": String},
    },
    required=("sha", "referenced_elsewhere_locally"),
)

_PURGE_SNAPSHOT = _object(
    {"snapshot_id": String, "doc_id": String},
    required=("snapshot_id", "doc_id"),
)

_PURGE_PLAN_RESULT = _object(
    {
        "plan_id": String,
        "space_id": String,
        "meta_frontier": String,
        "docs": {"type": "array", "items": _PURGE_DOCUMENT},
        "blobs": {"type": "array", "items": _PURGE_BLOB},
        "snapshots": {"type": "array", "items": _PURGE_SNAPSHOT},
        "writers_affected": {"type": "array", "items": String},
        "authors_before": {
            "type": "object",
            "additionalProperties": {"type": "integer"},
        },
        "expires_at": String,
    },
    required=(
        "plan_id",
        "space_id",
        "meta_frontier",
        "docs",
        "blobs",
        "snapshots",
        "writers_affected",
        "authors_before",
        "expires_at",
    ),
)

_PURGE_REPLACEMENT = _object(
    {
        "doc_id": String,
        "replacement_doc_id": String,
        "snapshot_id": String,
        "snapshot_sha256": String,
    },
    required=("doc_id", "replacement_doc_id", "snapshot_id", "snapshot_sha256"),
)

_PURGE_RESULT = _object(
    {
        "plan_id": String,
        "executed_locally": _BOOL,
        "acknowledged": {"type": "array", "items": String},
        "pending": {"type": "array", "items": String},
        "replacements": {"type": "array", "items": _PURGE_REPLACEMENT},
    },
    required=(
        "plan_id",
        "executed_locally",
        "acknowledged",
        "pending",
        "replacements",
    ),
)

_SPACE_PURGE_RESULT = _object(
    {"spaceId": String, "purged": _BOOL, "localOnly": _BOOL},
    required=("spaceId", "purged", "localOnly"),
)

_SERVE_RESULT = _object(
    {
        "spaceId": String,
        "backend": {"type": "string", "enum": ["fs", "memory"]},
        "durable": _BOOL,
        "pinnedBlobs": {"type": "array", "items": String},
    },
    required=("spaceId", "backend", "durable", "pinnedBlobs"),
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


ORGFS_CREATE_SPEC = _write_spec("orgfs.create", _CREATE_PARAMS, SPACE)
ORGFS_WRITE_SPEC = _write_spec("orgfs.write", _WRITE_PARAMS, NODE)
ORGFS_EXPORT_SPEC = _write_spec(
    "orgfs.export",
    _EXPORT_PARAMS,
    NODE,
    # The path policy authenticates ``from`` (address admission).
    errors=PATH_ERRORS + ORG_CONTEXT_ERRORS + (AGENT_ADDRESS_RETIRED_ERROR,),
    identity=IdentityBinding("local-owner-selector", "from"),
)
ORGFS_IMPORT_SPEC = _write_spec(
    "orgfs.import",
    _IMPORT_PARAMS,
    NODE,
    errors=PATH_ERRORS + SESSION_ERRORS + (AGENT_ADDRESS_RETIRED_ERROR,),
    identity=IdentityBinding("local-owner-selector", "from"),
)
ORGFS_MKDIR_SPEC = _write_spec("orgfs.mkdir", _MKDIR_PARAMS, NODE)
ORGFS_MOVE_SPEC = _write_spec("orgfs.move", _MOVE_PARAMS, NODE)
ORGFS_REMOVE_SPEC = _write_spec("orgfs.remove", _REMOVE_PARAMS, _OK)
ORGFS_RESTORE_SPEC = _write_spec("orgfs.restore", _RESTORE_PARAMS, NODE)
ORGFS_PURGE_PLAN_SPEC = _write_spec(
    "orgfs.purge_plan", _PURGE_PLAN_PARAMS, _PURGE_PLAN_RESULT, errors=PURGE_ERRORS
)
ORGFS_PURGE_SPEC = _write_spec(
    "orgfs.purge", _PURGE_PARAMS, _PURGE_RESULT, errors=PURGE_ERRORS
)
ORGFS_SPACE_PURGE_SPEC = _write_spec(
    "orgfs.space_purge", _SPACE_PARAMS, _SPACE_PURGE_RESULT, errors=PURGE_ERRORS
)
ORGFS_UNBAN_SPEC = _write_spec(
    "orgfs.unban", _UNBAN_PARAMS, _OK, errors=PURGE_ERRORS
)
ORGFS_SERVE_SPEC = _write_spec("orgfs.serve", _SERVE_PARAMS, _SERVE_RESULT)
ORGFS_CHECKOUT_SPEC = _write_spec(
    "orgfs.checkout", _CHECKOUT_PARAMS, _CHECKOUT_RESULT
)
ORGFS_INVITE_SPEC = _write_spec("orgfs.invite", _INVITE_PARAMS, MEMBER)
ORGFS_REMOVE_MEMBER_SPEC = _write_spec(
    "orgfs.remove_member", _REMOVE_MEMBER_PARAMS, _OK
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
    (ORGFS_PURGE_PLAN_SPEC, _handler("orgfs.purge_plan")),
    (ORGFS_PURGE_SPEC, _handler("orgfs.purge")),
    (ORGFS_SPACE_PURGE_SPEC, _handler("orgfs.space_purge")),
    (ORGFS_UNBAN_SPEC, _handler("orgfs.unban")),
    (ORGFS_SERVE_SPEC, _handler("orgfs.serve")),
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
    "ORGFS_PURGE_PLAN_SPEC",
    "ORGFS_PURGE_SPEC",
    "ORGFS_SPACE_PURGE_SPEC",
    "ORGFS_UNBAN_SPEC",
    "ORGFS_SERVE_SPEC",
    "ORGFS_CHECKOUT_SPEC",
    "ORGFS_INVITE_SPEC",
    "ORGFS_REMOVE_MEMBER_SPEC",
    "ORGFS_JOIN_SPEC",
]
