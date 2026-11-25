"""Compatibility declaration for the existing invite join service.

OrgFS error spellings and structured details are preserved, not renamed to
DAEMON_ERROR during migration. No second join implementation lives here.
"""
from collections.abc import Mapping
from typing import Any

from hyprial.daemon.impl.org.network.ipc import ipc_org_join
from hyprial.daemon.impl.orgfs.api import ORGFS_ERROR_CODES
from hyprial.kernel import IdempotencySpec, MethodError, MethodSpec, ipc_errors

# Existing sidecar membership, transport and org-network failure vocabulary.
# Kept explicit: unknown producer codes still trip the registry's closed check.
_JOIN_CODES = {
    ipc_errors.INVALID_ARGUMENT, ipc_errors.DAEMON_NOT_READY, "ORG_NETWORK_ERROR",
    "ORG_ACTION_NOT_AUTHORIZED", "DEVICE_RECORD_MISSING", "FORWARDING_UNAVAILABLE",
    "FORWARD_PROTOCOL", "FORWARD_UNAVAILABLE", ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT,
    "MEMBERSHIP_UNAVAILABLE", "INVITE_MALFORMED", "INVITE_DIGEST_MISMATCH",
    "INVITE_EXPIRED", "INVITE_SCHEME", "INVITE_VERSION_UNSUPPORTED",
    "AUTHORITY_PIN_MISMATCH", "AUTHORITY_REQUIRED", "AUTHORITY_UNREACHABLE",
    "BAD_REQUEST", "CONFIRMATION_REQUIRED", "INTERNAL", "INVITE_CONSUMED",
    "INVITE_INVALID", "MEMBERSHIP_ALREADY_INITIALIZED", "MEMBERSHIP_BACKUP_FAILED",
    "MEMBERSHIP_LEASE_EXPIRED", "MEMBERSHIP_LIMIT_EXCEEDED",
    "MEMBERSHIP_NOT_INITIALIZED", "MEMBERSHIP_STORE_INVALID", "MEMBER_CONFLICT",
    "MEMBER_REVOKED", "MEMBER_UNKNOWN", "NETWORK_NOT_QUIESCENT",
    "OPERATION_CONFLICT", "PEER_UPGRADE_REQUIRED", "REVISION_CONFLICT",
    "ROSTER_ROLLBACK", "SIDECAR_PROTOCOL_MISMATCH",
    ipc_errors.IDENTITY_CONFLICT,
}
_ORGFS_CODES = ORGFS_ERROR_CODES | {"ambiguous-space", "already-exists"}
# OrgFS has a legacy details map. Freeze its primitive/array value vocabulary
# rather than silently dropping it, or admitting arbitrary JSON with `true`.
_DETAIL_VALUE = {
    "anyOf": [
        {"type": ["string", "number", "boolean", "null"]},
        {"type": "array", "items": {"type": ["string", "number", "boolean", "null"]}},
    ]
}
_ORGFS_DATA = {
    "type": "object", "properties": {}, "additionalProperties": _DETAIL_VALUE,
}
JOIN_ERRORS = tuple(
    MethodError(
        code,
        _ORGFS_DATA if code in _ORGFS_CODES else None,
        description="Existing join service failure; spelling and details are preserved.",
    )
    for code in sorted(_JOIN_CODES | _ORGFS_CODES)
)

JOIN_STEP = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "minLength": 1},
        "ok": {"type": "boolean"},
        "detail": {"type": "string"},
    },
    "required": ["kind", "ok", "detail"],
    "additionalProperties": False,
}
ORG_JOIN_SPEC = MethodSpec(
    name="org.join",
    owner="daemon.org",
    params_schema={
        "type": "object",
        "properties": {
            "link": {"type": "string", "minLength": 1},
        },
        "required": ["link"],
        "additionalProperties": False,
    },
    result_schema={
        "type": "object",
        "properties": {
            "org": {"type": "string", "minLength": 1},
            "spaceId": {"type": "string", "minLength": 1},
            "steps": {"type": "array", "items": JOIN_STEP},
        },
        "required": ["org", "spaceId", "steps"],
        "additionalProperties": False,
    },
    errors=JOIN_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator", "bound-agent"),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="explicit-org-join"),
)


def org_join(app: Any, params: Mapping[str, Any], *, trusted_message_origin=None):
    return ipc_org_join(app, dict(params))


ORG_JOIN_BINDINGS = ((ORG_JOIN_SPEC, org_join),)
