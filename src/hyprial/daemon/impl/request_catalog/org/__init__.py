"""The first migrated ``org.*`` methods: declarations plus real handlers.

Decision record (org-members batch 1, 2026-11-02)
- Only ``org.list`` and ``org.fetch`` are declared.  The org family stays
  outside ``MIGRATED_FAMILIES`` because the remaining ``org.*`` names still
  live in the legacy router; closing the family would turn them all into
  ``METHOD_NOT_FOUND``.
- Both handlers forward to the existing implementations instead of copying
  their logic: ``ipc_org_list`` for the read, ``app._ipc_org_fetch`` for the
  write.  The declarations describe what those handlers already return.
- ``org.fetch`` is a write: it stages candidates in this node's org-context
  store.  It is declared ``mutates=True``/``retry=never``.  ``requestMode``
  stays optional: the default batch path and the explicit ``neighbors`` path
  reach the same handler through both IPC and HTTP.
- Error codes are declared with the data the handlers actually attach.  The
  historical lowercase OrgFS codes reachable on these paths are named through
  the shared legacy exception list (never by widening the uppercase grammar).
  That includes ``ambiguous-space``, ``unknown-space`` and ``unavailable``;
  ``org.fetch`` also exposes ``content-pending`` from a replicated document
  whose bytes have not arrived yet.  Every declared code keeps its meaning and
  data.
- ``org.fetch`` candidate enumeration and staging go through
  ``OrgContextOrgFsBridge._candidate_users``/``_stage_candidate``, whose
  ``listdir``/``read_text_snapshot`` calls surface two more real producers:
  ``invalid-argument`` (message-only, e.g. ``candidates`` is a text file, so
  ``listdir`` refuses a non-directory) and ``ambiguous-path`` (two same-named
  nodes).  Both reach the wire through the OrgFS exception, whose ``details``
  are not the IPC ``data`` field, so the wire carries the code and message
  only; declaring a data schema would describe data that does not exist.
  ``unknown-doc`` is consumed locally (empty enumeration / skipped candidate)
  and never reaches the wire, while ``not-a-member``/``not-owner`` are
  write-path guards not reachable from the read/stage path, so they stay
  undeclared.

Decision record (org lifecycle batch 2, 2026-11-02)
- Only ``org.create``, ``org.invite`` and ``org.join`` are added: they are the
  three lifecycle actions a daemon method really serves.  Reject/decline,
  rename/update, pending-invite listing and claim have no daemon method and
  stay unavailable instead of being emulated here.
- ``mode`` is ``unary``: the declaration vocabulary is ``unary``/``cursor-poll``
  (``MethodMode``), so "write" is expressed by ``mutates=True``,
  ``retry=never`` and ``confirmation=none`` exactly as ``org.fetch`` does.
  The handlers forward to the existing ``ipc_org_create``/``ipc_org_invite``/
  ``ipc_org_join``; no lifecycle logic is copied.
- Result shapes were read back from a real isolated daemon over AF_UNIX, not
  from a fixture: create returns the two spaces (``space``/``aclSpace`` with
  ``spaceId``/``name``/``owner``/``createdAt``), the idempotency flags
  ``existed``/``aclExisted``, ``casdoorGroup`` and both publish flags;
  invite returns the three link forms plus ``member``/``aclMember`` modes;
  join returns the executed bootstrap steps (``kind``/``ok``/``detail``).
- Errors were audited along the handler call graph.  A missing org on
  ``org.invite`` is NOT ``unknown-space``: the ACL policy fails closed first
  and the daemon answers ``ORG_ACTION_NOT_AUTHORIZED``.  ``unknown-space``
  stays declared for the real disappearing-space races (the policy/doc read
  succeeded, then the directory or ACL space was gone) and for a bound ACL id
  that no longer resolves.  ``ambiguous-space`` exists only on the create
  path (``_find_space`` and the ACL resume scan); the invite pre-check reads
  the policy first and answers ``ORG_ACTION_NOT_AUTHORIZED`` instead.
- The lowercase codes that reach the wire on these paths are
  ``ambiguous-space``, ``unknown-space``, ``unavailable``, ``no-holder-online``
  and ``not-a-member``; all are members of the frozen legacy list.  Their
  data schemas are the union of their real producers, never an open object.
  Exotic store rejections these paths do not produce (``stale-write``,
  ``projection-behind``, ``too-large``, ``resource-exhausted``) are not
  declared; if one is ever raised the registry's documented fallback is
  ``DAEMON_ERROR`` rather than a mis-mapped code.
- Tailnet/Casdoor failures keep their own uppercase codes with no data:
  create exposes ``DEVICE_RECORD_MISSING``, ``AUTHORITY_UNREACHABLE``,
  ``MEMBERSHIP_NOT_INITIALIZED``, ``MEMBERSHIP_STORE_INVALID``,
  ``MEMBERSHIP_SIDECAR_TIMEOUT``, ``BAD_REQUEST``,
  ``CASDOOR_GROUP_UNAVAILABLE`` and ``CASDOOR_ORG_QUOTA_EXCEEDED``; create
  also exposes the real lowercase ``invalid-argument`` raised when
  ``directory/org.json`` cannot be resumed, with an empty-object data schema
  (OrgFsError consumes its internal message key before the IPC boundary);
  invite exposes ``DEVICE_RECORD_MISSING``, ``DEVICE_ADDRESS_UNAVAILABLE``,
  ``CASDOOR_USER_NOT_FOUND``, ``CASDOOR_GROUP_WRITE_FAILED``,
  ``AUTHORITY_REQUIRED``, ``MEMBERSHIP_NOT_INITIALIZED``,
  ``MEMBERSHIP_STORE_INVALID`` and ``MEMBERSHIP_SIDECAR_TIMEOUT``; join
  exposes the invite decode/redemption codes ``INVITE_MALFORMED``,
  ``INVITE_VERSION_UNSUPPORTED``, ``INVITE_DIGEST_MISMATCH``,
  ``INVITE_EXPIRED``, ``INVITE_INVALID``, ``INVITE_CONSUMED``,
  ``MEMBERSHIP_NOT_INITIALIZED``, ``MEMBERSHIP_SIDECAR_TIMEOUT``,
  ``DEVICE_RECORD_MISSING``, ``AUTHORITY_PIN_MISMATCH`` and
  ``FORWARDING_UNAVAILABLE``.

Decision record (KP-041 org.join error data, 2026-11-19)
- The registry replaces a declared error whose data fails the declared schema
  with ``DAEMON_ERROR``, so every real join failure that attached producer
  fields was losing its code and diagnostics.  The join declarations now name
  the union of their real producers while staying closed
  (``additionalProperties: false``): the lifecycle wait timeout adds
  ``leg``/``budgetSeconds`` to ``unavailable``; the membership sidecar timeout
  declares ``operation``/``leg``/``budgetSeconds`` plus the closed transport
  diagnostic vocabulary (``stage``/``cause``/``proxy``/``attempts``/
  ``bytesReceived``); ``no-holder-online`` and ``not-a-member`` also carry the
  invite-redemption enrichment ``status``/``missingStep``/``inviteId`` used by
  the shared join vocabulary; and a malformed joined snapshot surfaces
  ``invalid-argument`` with an empty object or the rejected ``doc_id``.
  Unknown keys, wrong types and a timeout without its leg triple still fail
  closed.

Decision record (org authority listing, 2026-11-06)
- ``org.list[].authorityDevice`` is required but nullable. ``true`` and ``false``
  describe an observed local/non-local tailnet authority; ``null`` means the
  forwarding sidecar or its authority metadata was unavailable. The field is a
  presentation input for guarding destructive actions, never an authorization
  grant. A sidecar read failure degrades this one field and keeps the org row.
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

from .membership import MEMBERSHIP_BINDINGS
from .membership.schemas import LIST_RESULT
from .acl import BATCH6_ACL_BINDINGS
from .network_specs import (
    BATCH2_ORG_BINDINGS,
    ORG_NETWORK_SPEC,
    ORG_PUBLISH_SPEC,
    ORG_REPAIR_SPEC,
)
from .orgfs import BATCH3_ORGFS_BINDINGS, BATCH4_ORGFS_BINDINGS
from .view_specs import BATCH5_VIEW_BINDINGS
from .removal_specs import (
    BATCH1_ORG_BINDINGS,
    ORG_DELETE_SPEC,
    ORG_EXECUTE_SPEC,
    ORG_LEAVE_SPEC,
    ORG_REMOVE_SPEC,
)
from .wire import (
    AMBIGUOUS_SPACE_DATA as _AMBIGUOUS_SPACE_DATA,
    CREATE_ERRORS as _ORG_CREATE_ERRORS,
    EMPTY_PARAMS as _EMPTY_PARAMS,
    UNAVAILABLE_DATA as _UNAVAILABLE_DATA,
    UNKNOWN_SPACE_DATA as _UNKNOWN_SPACE_DATA,
)

JsonObject = dict[str, Any]

_ORG_NETWORK_ERROR = "ORG_NETWORK_ERROR"

_FETCH_PARAMS = {
    "type": "object",
    "properties": {
        "timeoutSeconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": 30,
            "default": 2,
        },
        "from": {"type": "string", "minLength": 1},
        "requestMode": {"type": "string", "enum": ["neighbors"]},
    },
    "additionalProperties": False,
}

_ORG_ROW = {
    "type": "object",
    "properties": {
        "org": {"type": "string", "minLength": 1},
        "spaceId": {"type": ["string", "null"]},
        "role": {"type": ["string", "null"]},
        "devices": {"type": "integer", "minimum": 0},
        "state": {"type": "string", "minLength": 1},
        "authorityDevice": {"type": ["boolean", "null"]},
        "parent": {"type": ["string", "null"]},
        "tailnetId": {"type": ["string", "null"]},
    },
    "required": ["org", "spaceId", "role", "devices", "state", "authorityDevice"],
    "additionalProperties": False,
}

_ORG_LIST_RESULT = {
    "type": "object",
    "properties": {
        "orgs": {"type": "array", "items": _ORG_ROW},
        "leftOrgs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["orgs", "leftOrgs"],
    "additionalProperties": False,
}

_ORG_FETCH_CANDIDATE = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "version": {"type": "integer", "minimum": 0},
        "publisher": {"type": "string"},
        "receivedAt": {"type": "string", "minLength": 1},
        "source": {"type": "string", "minLength": 1},
    },
    "required": ["path", "version", "publisher", "receivedAt", "source"],
    "additionalProperties": False,
}

_ORG_FETCH_RESULT = {
    "type": "object",
    "properties": {
        "requestedSources": {"type": "array", "items": {"type": "string"}},
        "responseCount": {"type": "integer", "minimum": 0},
        "receivedCount": {"type": "integer", "minimum": 0},
        "rejectedCount": {"type": "integer", "minimum": 0},
        "candidates": {"type": "array", "items": _ORG_FETCH_CANDIDATE},
        "source": {"const": "orgfs"},
        "refreshed": {"type": "boolean"},
        "spaceId": {"type": "string", "minLength": 1},
        "deprecatedSourceTarget": {"type": "boolean"},
        "sourceTargetHint": {"type": "string", "minLength": 1},
    },
    "required": [
        "requestedSources",
        "responseCount",
        "receivedCount",
        "rejectedCount",
        "candidates",
    ],
    "additionalProperties": False,
}

_AVAILABLE_SOURCES_DATA = {
    "type": "object",
    "properties": {
        "availableSources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["availableSources"],
    "additionalProperties": False,
}

_SPACE_HINT_DATA = {
    "type": "object",
    "properties": {"hint": {"type": "string"}},
    "required": ["hint"],
    "additionalProperties": False,
}

_SPACE_JSON = {
    "type": "object",
    "properties": {
        "spaceId": {"type": "string", "minLength": 1},
        "name": {"type": "string", "minLength": 1},
        "owner": {"type": "string", "minLength": 1},
        "createdAt": {"type": "string", "minLength": 1},
    },
    "required": ["spaceId", "name", "owner", "createdAt"],
    "additionalProperties": False,
}

_CREATE_PARAMS = {
    "type": "object",
    "properties": {
        "org": {"type": "string", "minLength": 1},
        "parent": {"type": "string", "minLength": 1},
    },
    "required": ["org"],
    "additionalProperties": False,
}

_INVITE_PARAMS = {
    "type": "object",
    "properties": {
        "org": {"type": "string", "minLength": 1},
        "user": {"type": "string", "minLength": 1},
    },
    "required": ["org", "user"],
    "additionalProperties": False,
}

_JOIN_PARAMS = {
    "type": "object",
    "properties": {"link": {"type": "string", "minLength": 1}},
    "required": ["link"],
    "additionalProperties": False,
}

_ORG_CREATE_RESULT = {
    "type": "object",
    "properties": {
        "org": {"type": "string", "minLength": 1},
        "spaceId": {"type": "string", "minLength": 1},
        "aclSpaceId": {"type": "string", "minLength": 1},
        "space": _SPACE_JSON,
        "aclSpace": _SPACE_JSON,
        "existed": {"type": "boolean"},
        "aclExisted": {"type": "boolean"},
        "casdoorGroup": {"type": "string", "enum": ["not-configured", "verified"]},
        "tailnetInitialized": {"type": "boolean"},
        "published": {"type": ["boolean", "null"]},
        "devicePublished": {"type": ["boolean", "null"]},
        "parent": {"type": ["string", "null"]},
        "parentSpaceId": {"type": "string", "minLength": 1},
        "tailnetId": {"type": "string", "minLength": 1},
    },
    "required": [
        "org",
        "spaceId",
        "aclSpaceId",
        "space",
        "aclSpace",
        "existed",
        "aclExisted",
        "casdoorGroup",
        "tailnetInitialized",
        "published",
        "devicePublished",
    ],
    "additionalProperties": False,
}

_ORG_INVITE_MEMBER = {
    "type": "object",
    "properties": {
        "spaceId": {"type": "string", "minLength": 1},
        "user": {"type": "string", "minLength": 1},
        "mode": {"type": "string", "enum": ["ro", "rw"]},
    },
    "required": ["spaceId", "user", "mode"],
    "additionalProperties": False,
}

_ORG_INVITE_RESULT = {
    "type": "object",
    "properties": {
        "org": {"type": "string", "minLength": 1},
        "spaceId": {"type": "string", "minLength": 1},
        "aclSpaceId": {"type": "string", "minLength": 1},
        "user": {"type": "string", "minLength": 1},
        "link": {"type": "string", "minLength": 1},
        "deepLink": {"type": "string", "minLength": 1},
        "command": {"type": "string", "minLength": 1},
        "invite": {"type": "string", "minLength": 1},
        "expiresAt": {"type": "string", "minLength": 1},
        "member": _ORG_INVITE_MEMBER,
        "aclMember": _ORG_INVITE_MEMBER,
    },
    "required": [
        "org",
        "spaceId",
        "aclSpaceId",
        "user",
        "link",
        "deepLink",
        "command",
        "invite",
        "expiresAt",
        "member",
        "aclMember",
    ],
    "additionalProperties": False,
}

_ORG_JOIN_STEP = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": [
                "connect-inviter",
                "join-org",
                "register-device",
                "connect-members",
            ],
        },
        "ok": {"const": True},
        "detail": {"type": "string"},
    },
    "required": ["kind", "ok", "detail"],
    "additionalProperties": False,
}

_ORG_JOIN_RESULT = {
    "type": "object",
    "properties": {
        "org": {"type": "string", "minLength": 1},
        "spaceId": {"type": "string", "minLength": 1},
        "steps": {"type": "array", "items": _ORG_JOIN_STEP},
    },
    "required": ["org", "spaceId", "steps"],
    "additionalProperties": False,
}

_INVITE_REDEMPTION_FIELDS = {
    "status": {"type": "string"},
    "missingStep": {"type": "string"},
    "inviteId": {"type": "string"},
}

_NO_HOLDER_DATA = {
    "type": "object",
    "properties": {
        "spaceId": {"type": "string"},
        "reason": {"type": "string"},
        "pages": {"type": "integer", "minimum": 0},
        "requestBytes": {"type": "integer", "minimum": 0},
        "responseBytes": {"type": "integer", "minimum": 0},
        "elapsedMs": {"type": "integer", "minimum": 0},
        **_INVITE_REDEMPTION_FIELDS,
    },
    "additionalProperties": False,
}

_NOT_A_MEMBER_DATA = {
    "type": "object",
    "properties": {
        "spaceId": {"type": "string"},
        "reason": {"type": "string"},
        **_INVITE_REDEMPTION_FIELDS,
    },
    "additionalProperties": False,
}

_JOIN_INVALID_ARGUMENT_DATA = {
    "type": "object",
    "properties": {"doc_id": {"type": "string"}},
    "additionalProperties": False,
}

_TRANSPORT_CAUSE = {
    "enum": [
        "proxy-rejected",
        "connection-refused",
        "dns-failed",
        "tls-failed",
        "deadline-exceeded",
        "transport-failed",
        "response-timeout",
    ]
}

_MEMBERSHIP_TIMEOUT_DATA = {
    "type": "object",
    "properties": {
        "operation": {"type": "string"},
        "leg": {"type": "string"},
        "budgetSeconds": {"type": "number"},
        "stage": {"enum": ["control", "derp", "sidecar"]},
        "cause": _TRANSPORT_CAUSE,
        "proxy": {"enum": ["configured", "not-configured"]},
        "attempts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "node": {"type": "string"},
                    "cause": _TRANSPORT_CAUSE,
                    "source": {"const": "diagnostic-probe"},
                },
                "required": ["node", "cause"],
                "additionalProperties": False,
            },
        },
        "bytesReceived": {"type": "integer", "minimum": 0},
    },
    "required": ["operation", "leg", "budgetSeconds"],
    "additionalProperties": False,
}

_CONTENT_PENDING_DATA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string"},
        "spaceId": {"type": "string"},
        "node": {"type": "string"},
        "path": {"type": "string"},
        "suggestedHolders": {"type": "array", "items": {"type": "string"}},
        "waitedSeconds": {"type": "number"},
        "requiredFrontier": {"type": ["string", "null"]},
        "localFrontier": {"type": ["string", "null"]},
        "expectedSize": {"type": ["integer", "null"]},
        "expectedSha256": {"type": ["string", "null"]},
        "localState": {"type": "string"},
    },
    "required": ["spaceId", "node"],
    "additionalProperties": False,
}

_ORG_LIST_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT, description="no data"),
    MethodError(ipc_errors.DAEMON_NOT_READY, description="no data"),
    MethodError(_ORG_NETWORK_ERROR, description="no data"),
    MethodError(
        "ambiguous-space",
        _AMBIGUOUS_SPACE_DATA,
        description="more than one space carries the org directory name",
    ),
    MethodError(
        "unknown-space",
        _UNKNOWN_SPACE_DATA,
        description="an org space disappeared while the list was being read",
    ),
    MethodError(
        "unavailable",
        _UNAVAILABLE_DATA,
        description="orgfs is closing or a space authority is not available",
    ),
)

_ORG_FETCH_ERRORS = (
    MethodError(
        ipc_errors.INVALID_ARGUMENT,
        description="a request argument does not match the shared IPC parameter shape",
    ),
    MethodError(
        ipc_errors.ORG_PATH_SHAPE_INVALID,
        description=(
            "candidate enumeration or reading rejected its path shape "
            "(for example, candidates is not a directory)"
        ),
    ),
    MethodError(
        ipc_errors.DAEMON_NOT_READY,
        description="the org-context endpoint or fetch source is not ready",
    ),
    MethodError(
        _ORG_NETWORK_ERROR,
        description="an unexpected org-network failure",
    ),
    MethodError(
        ipc_errors.ORG_SOURCE_UNREACHABLE,
        _AVAILABLE_SOURCES_DATA,
        description="the requested source is not online",
    ),
    MethodError(
        ipc_errors.ORG_SPACE_NOT_JOINED,
        _SPACE_HINT_DATA,
        description="this node has not joined the org-context space",
    ),
    MethodError(
        "ambiguous-space",
        _AMBIGUOUS_SPACE_DATA,
        description="more than one space carries the org directory name",
    ),
    MethodError(
        "ambiguous-path",
        description="a candidate path resolves to more than one node",
    ),
    MethodError(
        "unknown-space",
        _UNKNOWN_SPACE_DATA,
        description="the org-context space or a candidate space disappeared",
    ),
    MethodError(
        "unavailable",
        _UNAVAILABLE_DATA,
        description="orgfs is closing or a space authority is not available",
    ),
    MethodError(
        ipc_errors.ORGFS_CONTENT_PENDING,
        _CONTENT_PENDING_DATA,
        description="a candidate document exists but its bytes have not arrived",
    ),
)

ORG_LIST_SPEC = MethodSpec(
    name="org.list",
    owner="daemon.org",
    params_schema=_EMPTY_PARAMS,
    result_schema=LIST_RESULT,
    errors=_ORG_LIST_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator",),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)

ORG_FETCH_SPEC = MethodSpec(
    name="org.fetch",
    owner="daemon.org",
    params_schema=_FETCH_PARAMS,
    result_schema=_ORG_FETCH_RESULT,
    errors=_ORG_FETCH_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="org-context-fetch"),
)

_ORG_INVITE_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT, description="no data"),
    MethodError(
        "ORG_ACTION_NOT_AUTHORIZED",
        description=(
            "the ACL policy denies invite, or fails closed because the org, "
            "ACL binding or policy document is unavailable"
        ),
    ),
    MethodError(
        "unknown-space",
        _UNKNOWN_SPACE_DATA,
        description="the directory or ACL space disappeared during the invite",
    ),
    MethodError(
        "unavailable",
        _UNAVAILABLE_DATA,
        description="orgfs is closing while the invite is being written",
    ),
    MethodError(
        "DEVICE_RECORD_MISSING",
        description="no device record exists to sign the invite",
    ),
    MethodError(
        "DEVICE_ADDRESS_UNAVAILABLE",
        description="the sidecar has not written this device's Tailcat address",
    ),
    MethodError(
        "CASDOOR_USER_NOT_FOUND",
        description="Casdoor has no user with the invited login identity",
    ),
    MethodError(
        "CASDOOR_GROUP_WRITE_FAILED",
        description="adding the invited user to the Casdoor group failed",
    ),
    MethodError(
        "AUTHORITY_REQUIRED",
        description="only the tailnet authority may mint an invite",
    ),
    MethodError(
        "MEMBERSHIP_NOT_INITIALIZED",
        description="the membership sidecar or the tailnet is not initialized",
    ),
    MethodError(
        "MEMBERSHIP_STORE_INVALID",
        description="the membership store cannot be read",
    ),
    MethodError(
        ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT,
        description="invite-create exceeded the request-path sidecar budget",
    ),
    MethodError(
        ipc_errors.ORG_CELL_UNBOUND,
        description="legacy org without an org cell; see the migration guide",
    ),
    MethodError(
        ipc_errors.ORG_ALREADY_DELETED,
        description="the org is already deleted on this home",
    ),
)

_ORG_JOIN_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT, description="no data"),
    MethodError(
        "invalid-argument",
        _JOIN_INVALID_ARGUMENT_DATA,
        description=(
            "the link or a joined space's snapshot is invalid; the wire data "
            "is an empty object or names the rejected document id"
        ),
    ),
    MethodError(
        "INVITE_MALFORMED",
        description="the link is not one of the four accepted invite forms",
    ),
    MethodError(
        "INVITE_VERSION_UNSUPPORTED",
        description="the link carries a v1 invitation",
    ),
    MethodError(
        "INVITE_DIGEST_MISMATCH",
        description="workflow_sha256 does not match the embedded workflow",
    ),
    MethodError("INVITE_EXPIRED", description="the link or its tailnet invite expired"),
    MethodError(
        "INVITE_INVALID",
        description="the sidecar does not hold this invitation for this owner",
    ),
    MethodError(
        "INVITE_CONSUMED",
        description="the one-time invitation was already redeemed by another device",
    ),
    MethodError(
        "MEMBERSHIP_NOT_INITIALIZED",
        description="the membership sidecar or its store is not initialized",
    ),
    MethodError(
        ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT,
        _MEMBERSHIP_TIMEOUT_DATA,
        description=(
            "invite redemption or membership lookup exceeded the request-path "
            "budget; the wire data names the leg and the closed transport "
            "diagnostic classification"
        ),
    ),
    MethodError(
        "DEVICE_RECORD_MISSING",
        description="no device record exists to redeem the invite or map candidates",
    ),
    MethodError(
        "AUTHORITY_PIN_MISMATCH",
        description="the invite names an authority this node has not pinned",
    ),
    MethodError(
        "AUTHORITY_UNREACHABLE",
        description="the membership authority is unavailable while joining",
    ),
    MethodError(
        "FORWARDING_UNAVAILABLE",
        description="forwarding is not configured, so bootstrap peers cannot be mapped",
    ),
    MethodError(
        "no-holder-online",
        _NO_HOLDER_DATA,
        description="no other device holding a space answered within the join budget",
    ),
    MethodError(
        "not-a-member",
        _NOT_A_MEMBER_DATA,
        description="the supplier is not an online member of the space",
    ),
    MethodError(
        "unknown-space",
        _UNKNOWN_SPACE_DATA,
        description="a space named by the invite does not exist on this node yet",
    ),
    MethodError(
        "unavailable",
        _UNAVAILABLE_DATA,
        description="orgfs is closing while the join steps run",
    ),
    MethodError(
        "ORG_ACTION_NOT_AUTHORIZED",
        description="joining this sub-org requires confirmed parent membership",
    ),
    MethodError(
        ipc_errors.IDENTITY_CONFLICT,
        description="authenticated OIDC identity is unavailable or mismatches the roster",
    ),
)

ORG_CREATE_SPEC = MethodSpec(
    name="org.create",
    owner="daemon.org",
    params_schema=_CREATE_PARAMS,
    result_schema=_ORG_CREATE_RESULT,
    errors=_ORG_CREATE_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="org-create"),
)

ORG_INVITE_SPEC = MethodSpec(
    name="org.invite",
    owner="daemon.org",
    params_schema=_INVITE_PARAMS,
    result_schema=_ORG_INVITE_RESULT,
    errors=_ORG_INVITE_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="org-invite"),
)

ORG_JOIN_SPEC = MethodSpec(
    name="org.join",
    owner="daemon.org",
    params_schema=_JOIN_PARAMS,
    result_schema=_ORG_JOIN_RESULT,
    errors=_ORG_JOIN_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="org-join"),
)


def org_create(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_create

    return ipc_org_create(app, dict(params))


def org_invite(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_invite

    return ipc_org_invite(app, dict(params))


def org_join(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_join

    return ipc_org_join(app, dict(params))



def org_list(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from hyprial.daemon.impl.org.membership.context import for_app

    return for_app(app, repair_current=False).listing()


def org_fetch(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    handler = getattr(app, "_ipc_org_fetch", None)
    if handler is None:
        raise DaemonRequestError(
            ipc_errors.DAEMON_NOT_READY, "org-context fetch is not wired"
        )
    from hyprial.daemon.impl.orgfs.api import OrgFsError

    try:
        return handler(dict(params))
    except (DaemonRequestError, OrgFsError) as error:
        if error.code == "invalid-argument":
            raise DaemonRequestError(
                ipc_errors.ORG_PATH_SHAPE_INVALID, str(error)
            ) from error
        raise


ORG_BINDINGS = (
    (ORG_CREATE_SPEC, org_create),
    (ORG_FETCH_SPEC, org_fetch),
    (ORG_INVITE_SPEC, org_invite),
    (ORG_JOIN_SPEC, org_join),
    (ORG_LIST_SPEC, org_list),
) + MEMBERSHIP_BINDINGS + BATCH1_ORG_BINDINGS + BATCH2_ORG_BINDINGS + BATCH3_ORGFS_BINDINGS + BATCH4_ORGFS_BINDINGS + BATCH5_VIEW_BINDINGS + BATCH6_ACL_BINDINGS

__all__ = [
    "ORG_BINDINGS",
    "ORG_CREATE_SPEC",
    "ORG_DELETE_SPEC",
    "ORG_EXECUTE_SPEC",
    "ORG_FETCH_SPEC",
    "ORG_INVITE_SPEC",
    "ORG_JOIN_SPEC",
    "ORG_LEAVE_SPEC",
    "ORG_LIST_SPEC",
    "ORG_NETWORK_SPEC",
    "ORG_PUBLISH_SPEC",
    "ORG_REMOVE_SPEC",
    "ORG_REPAIR_SPEC",
    "org_create",
    "org_fetch",
    "org_invite",
    "org_join",
    "org_list",
]
