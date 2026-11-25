"""Org removal/execute MethodSpecs (org-lifecycle batch 1).

This batch declares the member lifecycle methods that really exist in the
legacy IPC router: execute (join alias), leave, remove and delete.  Each
handler forwards to the existing ``ipc_org_*`` entry point; no lifecycle or
authorization logic is copied.  ``org.delete`` is the OC7 two-phase deletion:
a preview call binds a durable journal operation, and only the confirmation
carrying its ``operationId`` executes (tombstone, tailnet shutdown, cleanup).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.kernel import (
    HttpBinding,
    IdentityBinding,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    ipc_errors,
)

JsonObject = dict[str, Any]

_ORG = {"type": "string", "minLength": 1}
_STRING = {"type": "string"}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer"}

_EMPTY_PARAMS = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_EXECUTE_PARAMS = {
    "type": "object",
    "properties": {"link": _ORG},
    "required": ["link"],
    "additionalProperties": False,
}

_LEAVE_PARAMS = {
    "type": "object",
    "properties": {
        "org": _ORG,
        "confirmed": _BOOL,
        "operationId": _ORG,
        "deviceIds": {"type": "array", "items": _ORG},
    },
    "required": ["org"],
    "additionalProperties": False,
}

_REMOVE_PARAMS = {
    "type": "object",
    "properties": {
        "org": _ORG,
        "user": _ORG,
        "confirmed": _BOOL,
        "operationId": _ORG,
        "deviceIds": {"type": "array", "items": _ORG},
    },
    "required": ["org", "user"],
    "additionalProperties": False,
}

_DELETE_PARAMS = {
    "type": "object",
    "properties": {
        "org": _ORG,
        "confirmed": _BOOL,
        "operationId": _ORG,
    },
    "required": ["org"],
    "additionalProperties": False,
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

#: ``no-holder-online`` and ``not-a-member`` producers disagree: the orgfs
#: facade emits ``{}`` or ``{spaceId}``, while the network sync timeout adds
#: transfer metrics.  Declaring the union keeps the code/data intact.
_NO_HOLDER_DATA = {
    "type": "object",
    "properties": {
        "spaceId": _STRING,
        "reason": _STRING,
        "pages": {"type": "integer", "minimum": 0},
        "requestBytes": {"type": "integer", "minimum": 0},
        "responseBytes": {"type": "integer", "minimum": 0},
        "elapsedMs": {"type": "integer", "minimum": 0},
    },
    "additionalProperties": False,
}

_NOT_A_MEMBER_DATA = {
    "type": "object",
    "properties": {"spaceId": _STRING, "reason": _STRING},
    "additionalProperties": False,
}

_SPACE_ONLY_DATA = {
    "type": "object",
    "properties": {"spaceId": _STRING},
    "additionalProperties": False,
}

_JOIN_STEP = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["connect-inviter", "join-org", "register-device", "connect-members"]},
        "ok": {"const": True},
        "detail": _STRING,
    },
    "required": ["kind", "ok", "detail"],
    "additionalProperties": False,
}

_JOIN_RESULT = {
    "type": "object",
    "properties": {
        "org": _ORG,
        "spaceId": _ORG,
        "steps": {"type": "array", "items": _JOIN_STEP},
    },
    "required": ["org", "spaceId", "steps"],
    "additionalProperties": False,
}

_REVISION_REFRESH = {
    "type": "object",
    "properties": {
        "oldRevision": _INT,
        "newRevision": _INT,
        "remainingDeviceIds": {"type": "array", "items": _ORG},
        "alreadyRemovedDeviceIds": {"type": "array", "items": _ORG},
        "unselectedDeviceIds": {"type": "array", "items": _ORG},
    },
    "required": [
        "oldRevision",
        "newRevision",
        "remainingDeviceIds",
        "alreadyRemovedDeviceIds",
        "unselectedDeviceIds",
    ],
    "additionalProperties": False,
}

_REVISION_AUDIT = {
    "type": "object",
    "properties": {
        "oldRevision": _INT,
        "newRevision": _INT,
        "remainingDeviceIds": {"type": "array", "items": _ORG},
        "alreadyRemovedDeviceIds": {"type": "array", "items": _ORG},
        "unselectedDeviceIds": {"type": "array", "items": _ORG},
        "confirmedAtMs": _INT,
        "operator": _STRING,
        "operatorDeviceId": _STRING,
    },
    "required": [
        "oldRevision",
        "newRevision",
        "remainingDeviceIds",
        "alreadyRemovedDeviceIds",
        "unselectedDeviceIds",
        "confirmedAtMs",
        "operator",
        "operatorDeviceId",
    ],
    "additionalProperties": False,
}

_REMOVAL_RESULT = {
    "type": "object",
    "properties": {
        "org": _ORG,
        "spaceId": _ORG,
        "user": _STRING,
        "casdoorGroup": _STRING,
        "operationId": _STRING,
        "tailnetId": _STRING,
        "owner": _STRING,
        "deviceIds": {"type": "array", "items": _ORG},
        "expectedRevision": _INT,
        "authorityDeviceId": _STRING,
        "role": _STRING,
        "phase": {
            "type": "string",
            "enum": [
                "pending-confirmation",
                "pending-authority",
                "revision-conflict",
                "revision-confirmation",
                "orgfs-pending",
                "tailnet-pending",
                "complete",
            ],
        },
        "reason": _STRING,
        "confirmed": _BOOL,
        "targetIdentityDigests": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "targetKeyDigests": {"type": "array", "items": _STRING},
        "revisionRefresh": _REVISION_REFRESH,
        "revisionAudit": {"type": "array", "items": _REVISION_AUDIT},
        "pendingDeviceIds": {"type": "array", "items": _ORG},
        "revisionConflictProof": _STRING,
        "upperLayerReceipt": _STRING,
        "zenohDeny": _STRING,
        "tailnetUnchanged": _BOOL,
        "result": {"type": "object"},
    },
    "required": ["org", "spaceId"],
    "additionalProperties": False,
}

_DELETE_RESULT = {
    "type": "object",
    "properties": {
        "org": _ORG,
        "spaceId": _ORG,
        "operationId": _ORG,
        "phase": {
            "type": "string",
            "enum": [
                "pending-confirmation",
                "orgfs-pending",
                "tailnet-pending",
                "cleanup-pending",
                "revision-conflict",
                "complete",
            ],
        },
        "deviceIds": {"type": "array", "items": _ORG},
        "confirmed": _BOOL,
        "irreversible": _BOOL,
        "migrationDoc": _STRING,
        "expectedRevision": _INT,
        "authorityDeviceId": _STRING,
        "orgfsMarked": _BOOL,
        "tailnetPhase": {"type": ["string", "null"]},
        "tailnetClosed": _BOOL,
        "tailnetForgotten": _BOOL,
        "deletedRecord": _STRING,
    },
    "required": [
        "org",
        "spaceId",
        "operationId",
        "phase",
        "deviceIds",
        "confirmed",
        "irreversible",
        "migrationDoc",
        "orgfsMarked",
        "tailnetPhase",
        "tailnetClosed",
        "tailnetForgotten",
    ],
    "additionalProperties": False,
}


def _error(code: str, data: Mapping[str, Any] | None = None, description: str = "") -> MethodError:
    return MethodError(code, data, description)


_INVITE_ERRORS = (
    _error(ipc_errors.INVALID_ARGUMENT, description="no data"),
    _error("INVITE_MALFORMED", description="not an accepted invite form"),
    _error("INVITE_VERSION_UNSUPPORTED", description="the link carries a v1 invitation"),
    _error("INVITE_DIGEST_MISMATCH", description="the embedded workflow digest does not match"),
    _error("INVITE_EXPIRED", description="the invitation expired"),
    _error("INVITE_INVALID", description="the sidecar does not hold this invitation"),
    _error("INVITE_CONSUMED", description="another device already redeemed it"),
    _error("MEMBERSHIP_NOT_INITIALIZED", description="the tailnet is not initialized"),
    _error(ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT, description="the sidecar missed the request budget"),
    _error("DEVICE_RECORD_MISSING", description="no local device record exists"),
    _error("AUTHORITY_PIN_MISMATCH", description="the invite names an unpinned authority"),
    _error("FORWARDING_UNAVAILABLE", description="forwarding is not configured"),
    _error(
        "no-holder-online",
        _NO_HOLDER_DATA,
        "no other device holding a space answered within the join budget",
    ),
    _error(
        "not-a-member",
        _NOT_A_MEMBER_DATA,
        "the supplier is not an online member of the space",
    ),
    _error("unknown-space", _UNKNOWN_SPACE_DATA, "the invited space does not exist here"),
    _error("unavailable", _UNAVAILABLE_DATA, "orgfs is closing"),
    _error("ORG_ACTION_NOT_AUTHORIZED", description="joining or accepting requires confirmed parent membership"),
    _error(ipc_errors.IDENTITY_CONFLICT, description="authenticated OIDC identity is unavailable or mismatches the roster"),
    _error("ORG_NETWORK_ERROR", description="an unexpected org-network failure"),
)

_REMOVAL_ERRORS = (
    _error(ipc_errors.INVALID_ARGUMENT, description="no data"),
    _error("ORG_ACTION_NOT_AUTHORIZED", description="the owner policy denies this removal"),
    _error("ambiguous-space", _AMBIGUOUS_SPACE_DATA, "more than one space carries the org name"),
    _error("unknown-space", _UNKNOWN_SPACE_DATA, "the org space disappeared"),
    _error("unavailable", _UNAVAILABLE_DATA, "orgfs is closing"),
    _error("LAST_AUTHORITY_REFUSED", description="an authority cannot be removed this way"),
    _error("SELF_REVOKE_REFUSED", description="the local device needs an explicit self-leave"),
    _error("REMOVAL_CONFIRMATION_STALE", description="the preview changed; confirm again"),
    _error("REVISION_CONFLICT", description="the sidecar revision changed; confirm again"),
    _error("OPERATION_CONFLICT", description="the journal cannot prove the original targets"),
    _error("MEMBER_CONFLICT", description="a target row conflicts with the journal"),
    _error("MEMBERSHIP_NOT_INITIALIZED", description="the tailnet is not initialized"),
    _error(ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT, description="the sidecar missed the request budget"),
    _error(
        "MEMBERSHIP_STORE_INVALID",
        description="the membership store cannot be read",
    ),
    _error("AUTHORITY_REQUIRED", description="resume this removal on the pinned authority"),
    _error(
        "AUTHORITY_UNREACHABLE",
        description="the forwarding supervisor has no live sidecar address",
    ),
    _error("DEVICE_RECORD_MISSING", description="no local device record exists"),
    _error("FORWARDING_UNAVAILABLE", description="forwarding is not configured"),
    _error(
        "SIDECAR_PROTOCOL_MISMATCH",
        description="the sidecar speaks an unsupported protocol version",
    ),
    _error(
        "CASDOOR_GROUP_WRITE_FAILED",
        description="removing the member from the Casdoor group failed",
    ),
    _error("not-owner", _SPACE_ONLY_DATA, "the daemon author does not own the space"),
    _error(
        "not-a-member",
        _NOT_A_MEMBER_DATA,
        "the space membership row is missing or read-only",
    ),
    _error(ipc_errors.ORG_CELL_UNBOUND, description="legacy org without an org cell; see the migration guide"),
    _error(ipc_errors.ORG_ALREADY_DELETED, description="the org is already deleted on this home"),
    _error("ORG_NETWORK_ERROR", description="an unexpected org-network failure"),
)

def org_execute(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_execute

    return ipc_org_execute(app, dict(params))


def org_leave(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_leave

    return ipc_org_leave(app, dict(params))


def org_remove(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_remove

    return ipc_org_remove(app, dict(params))


def org_delete(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_delete

    return ipc_org_delete(app, dict(params))


ORG_EXECUTE_SPEC = MethodSpec(
    name="org.execute",
    owner="daemon.org",
    params_schema=_EXECUTE_PARAMS,
    result_schema=_JOIN_RESULT,
    errors=_INVITE_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="org-execute"),
)

ORG_LEAVE_SPEC = MethodSpec(
    name="org.leave",
    owner="daemon.org",
    params_schema=_LEAVE_PARAMS,
    result_schema=_REMOVAL_RESULT,
    errors=_REMOVAL_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="org-leave"),
)

ORG_REMOVE_SPEC = MethodSpec(
    name="org.remove",
    owner="daemon.org",
    params_schema=_REMOVE_PARAMS,
    result_schema=_REMOVAL_RESULT,
    errors=_REMOVAL_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="org-remove"),
)

ORG_DELETE_SPEC = MethodSpec(
    name="org.delete",
    owner="daemon.org",
    params_schema=_DELETE_PARAMS,
    result_schema=_DELETE_RESULT,
    errors=(
        _error(ipc_errors.INVALID_ARGUMENT, description="no data"),
        _error("ORG_ACTION_NOT_AUTHORIZED", description="only the owner may delete"),
        _error(ipc_errors.ORG_ALREADY_DELETED, description="the org is already deleted on this home"),
        _error("REMOVAL_CONFIRMATION_STALE", description="the preview changed; confirm again"),
        _error("REVISION_CONFLICT", description="the tailnet revision changed; re-run the preview"),
        _error("ambiguous-space", _AMBIGUOUS_SPACE_DATA, "more than one space carries the name"),
        _error("unknown-space", _UNKNOWN_SPACE_DATA, "the org space disappeared"),
        _error("unavailable", _UNAVAILABLE_DATA, "orgfs is closing"),
        _error("MEMBERSHIP_NOT_INITIALIZED", description="the tailnet is not initialized"),
        _error(ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT, description="the sidecar missed the request budget"),
        _error(
            "MEMBERSHIP_STORE_INVALID",
            description="the membership store cannot be read",
        ),
        _error("AUTHORITY_REQUIRED", description="delete runs on the pinned tailnet authority"),
        _error("AUTHORITY_UNREACHABLE", description="the forwarding supervisor has no live sidecar address"),
        _error("OPERATION_CONFLICT", description="the sidecar holds a different receipt for this operation"),
        _error("MEMBERSHIP_LIMIT_EXCEEDED", description="the membership frame exceeds the sidecar limit"),
        _error("BAD_REQUEST", description="the sidecar rejected the membership request"),
        _error("FORWARD_PROTOCOL", description="the forwarding sidecar exited or closed its pipe"),
        _error(
            "SIDECAR_PROTOCOL_MISMATCH",
            description="the sidecar speaks an unsupported protocol version",
        ),
        _error(
            "MEMBERSHIP_BACKUP_FAILED",
            description="the tailnet shutdown backup could not be written",
        ),
        _error("ORG_NETWORK_ERROR", description="an unexpected org-network failure"),
    ),
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="org-delete"),
)


BATCH1_ORG_BINDINGS = (
    (ORG_DELETE_SPEC, org_delete),
    (ORG_EXECUTE_SPEC, org_execute),
    (ORG_LEAVE_SPEC, org_leave),
    (ORG_REMOVE_SPEC, org_remove),
)

__all__ = [
    "BATCH1_ORG_BINDINGS",
    "ORG_DELETE_SPEC",
    "ORG_EXECUTE_SPEC",
    "ORG_LEAVE_SPEC",
    "ORG_REMOVE_SPEC",
    "org_delete",
    "org_execute",
    "org_leave",
    "org_remove",
]
