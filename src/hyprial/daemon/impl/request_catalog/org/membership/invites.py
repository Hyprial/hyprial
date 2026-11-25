"""The migrated ``org.invites.*`` methods (§3 O2c).

Two declarations, one private service.  The account snapshot is a caller
parameter — the credential never crosses this boundary — and the answer is
display data only: no payload and no token appear in either schema, and the
``INVITE_*`` codes keep the codes the join path already publishes.

``org.join`` is declared in ``join.py``. Its existing error vocabulary is
shared by the accept path so OrgFS and sidecar failures keep their codes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from functools import partial
from typing import Any

from hyprial.daemon.impl.org.network.ipc import (
    ipc_org_invites_accept,
    ipc_org_invites_list,
)
from hyprial.kernel import (
    HttpBinding,
    IdentityBinding,
    IdempotencySpec,
    MethodSpec,
)

from .join import JOIN_ERRORS, JOIN_STEP

JsonObject = dict[str, Any]

ORG_INVITES_LIST = "org.invites.list"
ORG_INVITES_ACCEPT = "org.invites.accept"

_ACCOUNT_ROW = {
    "type": "object",
    "properties": {
        "payload": {"type": "string", "minLength": 1},
        "acceptedAt": {"type": ["string", "null"]},
    },
    "required": ["payload"],
    "additionalProperties": False,
}

_LIST_PARAMS = {
    "type": "object",
    "properties": {
        "invites": {"type": "array", "items": _ACCOUNT_ROW},
    },
    "required": ["invites"],
    "additionalProperties": False,
}

_PENDING_ROW = {
    "type": "object",
    "properties": {
        "inviteId": {"type": "string", "minLength": 1},
        "org": {"type": "string", "minLength": 1},
        "inviter": {"type": "string", "minLength": 1},
        "spaceId": {"type": "string", "minLength": 1},
        "acceptedAt": {"type": ["string", "null"]},
        "status": {"const": "partial"},
        "missingStep": {"type": "string", "minLength": 1},
    },
    "required": ["inviteId", "org", "inviter", "spaceId"],
    "additionalProperties": False,
}

_LIST_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "pending": {"type": "array", "items": _PENDING_ROW},
        "settled": {"type": "integer", "minimum": 0},
        "duplicates": {"type": "integer", "minimum": 0},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["ok", "pending", "settled", "duplicates", "warnings"],
    "additionalProperties": False,
}

_ACCEPT_PARAMS = {
    "type": "object",
    "properties": {
        "inviteId": {"type": "string", "minLength": 1},
        "link": {"type": "string", "minLength": 1},
    },
    "required": ["inviteId", "link"],
    "additionalProperties": False,
}

_STEP_ROW = JOIN_STEP

_ACCEPT_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "inviteId": {"type": "string", "minLength": 1},
        "org": {"type": "string", "minLength": 1},
        "spaceId": {"type": "string", "minLength": 1},
        "joined": {"type": "boolean"},
        "alreadyMember": {"type": "boolean"},
        "steps": {"type": "array", "items": _STEP_ROW},
    },
    "required": [
        "ok",
        "inviteId",
        "org",
        "spaceId",
        "joined",
        "alreadyMember",
        "steps",
    ],
    "additionalProperties": False,
}

_LIST_ERRORS = JOIN_ERRORS
_PARTIAL_DATA = {
    "type": "object",
    "properties": {
        "status": {"const": "partial"},
        "missingStep": {"type": "string", "minLength": 1},
        "inviteId": {"type": "string", "minLength": 1},
    },
    "required": ["status", "missingStep", "inviteId"],
    "additionalProperties": False,
}
_ACCEPT_ERRORS = tuple(
    replace(error, data_schema=_PARTIAL_DATA) if error.data_schema is None else error
    for error in JOIN_ERRORS
)

ORG_INVITES_LIST_SPEC = MethodSpec(
    name=ORG_INVITES_LIST,
    owner="daemon.org",
    params_schema=_LIST_PARAMS,
    result_schema=_LIST_RESULT,
    errors=_LIST_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator", "bound-agent"),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)

ORG_INVITES_ACCEPT_SPEC = MethodSpec(
    name=ORG_INVITES_ACCEPT,
    owner="daemon.org",
    params_schema=_ACCEPT_PARAMS,
    result_schema=_ACCEPT_RESULT,
    errors=_ACCEPT_ERRORS,
    mode="unary",
    mutates=True,
    retry="idempotency-key",
    audience=("local-operator", "bound-agent"),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(
        mode="idempotency-key",
        key_field="inviteId",
        scope="org-invite-accept",
    ),
)


def org_invites_list(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return ipc_org_invites_list(app, dict(params))


def org_invites_accept(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    return ipc_org_invites_accept(app, dict(params))


def _invites_param_error(method: str, error: Any, params: Mapping[str, Any]) -> str:
    validator = getattr(error, "validator", None)
    field = ""
    if validator == "required":
        missing = [
            name for name in getattr(error, "validator_value", ()) if name not in params
        ]
        field = missing[0] if missing else ""
    else:
        path = list(getattr(error, "absolute_path", ()))
        field = str(path[-1]) if path else ""
    if field in {"invites", "inviteId", "link"}:
        return f"{field} must be a non-empty value"
    path_text = "/".join(str(part) for part in getattr(error, "absolute_path", ())) or (
        "<root>"
    )
    return f"{method} params do not match the declared schema at {path_text}"


ORG_INVITE_BINDINGS = (
    (
        ORG_INVITES_LIST_SPEC,
        org_invites_list,
        partial(_invites_param_error, ORG_INVITES_LIST),
    ),
    (
        ORG_INVITES_ACCEPT_SPEC,
        org_invites_accept,
        partial(_invites_param_error, ORG_INVITES_ACCEPT),
    ),
)

__all__ = [
    "ORG_INVITES_ACCEPT",
    "ORG_INVITES_ACCEPT_SPEC",
    "ORG_INVITES_LIST",
    "ORG_INVITES_LIST_SPEC",
    "ORG_INVITE_BINDINGS",
    "org_invites_accept",
    "org_invites_list",
]
