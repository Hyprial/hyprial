"""Batch 6: owner-node ``org.acl.*`` MethodSpecs.

ACL grants are not a GUI surface (user ruling 2026-11-02).  These methods are
declared for the daemon/CLI contract only and are deliberately **not HTTP
exposed**: the existing service authenticates the owner terminal
through the IPC session fence, and this build has no trusted HTTP host binding
for that gate.  Declaring them IPC-only is the explicit gap; do not widen the
owner gate to make a browser call succeed.

UA1g: ``org.acl.grant`` declares USER_AGENT_IMMUTABLE; its handler refuses
the owner's user agent as the grantee after the owner checks.
``org.acl.revoke`` does not (PR 2941 second review): a grant an older build
wrote for the user agent must stay removable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.kernel import (
    IdempotencySpec,
    MethodError,
    MethodSpec,
    ipc_errors,
)

from ..orgfs.wire import SESSION_PROPERTIES

JsonObject = dict[str, Any]

_ORG = {"type": "string", "minLength": 1}
_STRING = {"type": "string"}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer"}

_ACL_FORBIDDEN_DATA = {
    "type": "object",
    "properties": {
        "method": _STRING,
        "caller": _STRING,
        "org": _STRING,
        "reason": _STRING,
    },
    # Two real producer shapes: the read path names the method/caller, the
    # owner-node grant/revoke path names the org and not-owner-node reason.
    "anyOf": [
        {"type": "object", "required": ["method"]},
        {"type": "object", "required": ["org", "reason"]},
    ],
    "additionalProperties": False,
}

_ACL_NOT_OWNED_DATA = {
    "type": "object",
    "properties": {"agent": _STRING},
    "required": ["agent"],
    "additionalProperties": False,
}

_ACL_INVALID_ARGUMENT_DATA = {
    "type": "object",
    "properties": {"untilMs": _INT},
    # The producer also attaches a clock reading; keep it out of the frozen
    # schema text (the projection gate forbids runtime clock field names).
    "additionalProperties": {"type": ["integer", "string", "null"]},
}

_AMBIGUOUS_SPACE_DATA = {
    "type": "object",
    "properties": {
        "org": _STRING,
        "space": _STRING,
        "count": {"type": "integer", "minimum": 1},
    },
    "additionalProperties": False,
    "anyOf": [
        {"type": "object", "required": ["space", "count"]},
        {"type": "object", "required": ["org"]},
    ],
}

_UNKNOWN_SPACE_DATA = {
    "type": "object",
    "properties": {
        "org": _STRING,
        "space": _STRING,
        "spaceId": _STRING,
        "reason": _STRING,
    },
    "additionalProperties": False,
    "anyOf": [
        {"type": "object", "required": ["spaceId"]},
        {"type": "object", "required": ["org", "space"]},
        {"type": "object", "required": ["org", "reason"]},
    ],
}

_UNAVAILABLE_DATA = {
    "type": "object",
    "properties": {"message": _STRING, "spaceId": _STRING},
    "additionalProperties": False,
}

_ACL_LS_PARAMS = {
    "type": "object",
    "properties": {**SESSION_PROPERTIES, "org": _ORG, "from": _ORG},
    "required": ["org", "from"],
    "additionalProperties": False,
}

_ACL_GRANT_PARAMS = {
    "type": "object",
    "properties": {
        **SESSION_PROPERTIES,
        "org": _ORG,
        "agent": _ORG,
        "untilMs": _INT,
        "from": _ORG,
    },
    "required": ["org", "agent", "from"],
    "additionalProperties": False,
}

_ACL_REVOKE_PARAMS = {
    "type": "object",
    "properties": {**SESSION_PROPERTIES, "org": _ORG, "agent": _ORG, "from": _ORG},
    "required": ["org", "agent", "from"],
    "additionalProperties": False,
}

_ACL_GRANT = {
    "type": "object",
    "properties": {
        "agent": _ORG,
        "grantedAtMs": _INT,
        "untilMs": {"type": ["integer", "null"]},
    },
    "required": ["agent", "grantedAtMs", "untilMs"],
    "additionalProperties": False,
}

_ACL_GRANT_RESULT = {
    "type": "object",
    "properties": {
        "org": _ORG,
        "agent": _ORG,
        "grantedAtMs": _INT,
        "untilMs": {"type": ["integer", "null"]},
    },
    "required": ["org", "agent", "grantedAtMs", "untilMs"],
    "additionalProperties": False,
}

_ACL_REVOKE_RESULT = {
    "type": "object",
    "properties": {"org": _ORG, "agent": _ORG, "revoked": _BOOL},
    "required": ["org", "agent", "revoked"],
    "additionalProperties": False,
}

_ACL_LS_RESULT = {
    "type": "object",
    "properties": {
        "org": _ORG,
        "grants": {"type": "array", "items": _ACL_GRANT},
    },
    "required": ["org", "grants"],
    "additionalProperties": False,
}


def _error(
    code: str, data: Mapping[str, Any] | None = None, description: str = ""
) -> MethodError:
    return MethodError(code, data, description)


_ACL_READ_ERRORS = (
    _error(
        ipc_errors.INVALID_ARGUMENT,
        _ACL_INVALID_ARGUMENT_DATA,
        "an ACL argument is not valid",
    ),
    _error(
        ipc_errors.ORG_ACL_GRANT_FORBIDDEN,
        _ACL_FORBIDDEN_DATA,
        "only the owner terminal may read grants",
    ),
    _error("ambiguous-space", _AMBIGUOUS_SPACE_DATA, "more than one space carries the org name"),
    _error("unknown-space", _UNKNOWN_SPACE_DATA, "the org space disappeared"),
    _error("unavailable", _UNAVAILABLE_DATA, "orgfs is closing"),
    _error("ORG_NETWORK_ERROR", description="an unexpected org-network failure"),
)

_ACL_WRITE_ERRORS = _ACL_READ_ERRORS + (
    _error(
        ipc_errors.ORG_ACL_GRANT_NOT_OWNED,
        _ACL_NOT_OWNED_DATA,
        "the agent is not registered to this owner",
    ),
)

_ACL_GRANT_ERRORS = _ACL_WRITE_ERRORS + (
    MethodError(ipc_errors.USER_AGENT_IMMUTABLE, description="The named agent is the owner's user agent, which is never administered; no data payload."),
)


def org_acl_ls(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_acl_ls

    return ipc_org_acl_ls(app, dict(params), trusted_origin=trusted_message_origin)


def org_acl_grant(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_acl_grant

    return ipc_org_acl_grant(app, dict(params), trusted_origin=trusted_message_origin)


def org_acl_revoke(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_acl_revoke

    return ipc_org_acl_revoke(app, dict(params), trusted_origin=trusted_message_origin)


ORG_ACL_LS_SPEC = MethodSpec(
    name="org.acl.ls",
    owner="daemon.org",
    params_schema=_ACL_LS_PARAMS,
    result_schema=_ACL_LS_RESULT,
    errors=_ACL_READ_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator",),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="org-acl-read"),
)

ORG_ACL_GRANT_SPEC = MethodSpec(
    name="org.acl.grant",
    owner="daemon.org",
    params_schema=_ACL_GRANT_PARAMS,
    result_schema=_ACL_GRANT_RESULT,
    errors=_ACL_GRANT_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="org-acl-grant"),
)

ORG_ACL_REVOKE_SPEC = MethodSpec(
    name="org.acl.revoke",
    owner="daemon.org",
    params_schema=_ACL_REVOKE_PARAMS,
    result_schema=_ACL_REVOKE_RESULT,
    errors=_ACL_WRITE_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="org-acl-revoke"),
)

BATCH6_ACL_BINDINGS = (
    (ORG_ACL_GRANT_SPEC, org_acl_grant),
    (ORG_ACL_LS_SPEC, org_acl_ls),
    (ORG_ACL_REVOKE_SPEC, org_acl_revoke),
)

__all__ = [
    "BATCH6_ACL_BINDINGS",
    "ORG_ACL_GRANT_SPEC",
    "ORG_ACL_LS_SPEC",
    "ORG_ACL_REVOKE_SPEC",
    "org_acl_grant",
    "org_acl_ls",
    "org_acl_revoke",
]
