"""Batch 2: ``org.network`` / ``org.repair`` / ``org.publish`` declarations.

Decision record (org lifecycle batch 2, 2026-11-02)
- Every handler forwards to the existing ``ipc_org_*`` entry point (or the
  application's ``_ipc_org_publish``); no lifecycle or authorization logic is
  copied here.
- ``org.network`` is a read (``mutates=False``, ``retry="safe"``); the other
  two are writes (``mutates=True``, ``retry="never"``).
- The result schemas were pinned against real isolated-daemon answers.  The
  optional network fields cover the panes the daemon may omit when a sidecar
  source is unavailable.
- 2026-11-25 (orgfs-minimal design section 5, package O): ``directoryPending``
  drops ``parkedCommits``.  The state host parks no commits, and an
  always-empty field would claim a property nothing measures; held-back
  updates are diagnosed through ``orgfs.status``.

Decision record (service grant S1, 2026-11-26: the published encryption key)
- A directory row may carry ``encryptionKeyId`` and ``encryptionPublicKey``, the
  public half of the publishing device's X25519 key.  Both are optional and
  never ``null``: the daemon answers either both or neither, and a ``null``
  would be a broken row.
- The schema does not tie the two to each other.  The result is checked
  against a closed schema at request time, and the pair rule has one
  enforcement point in the directory model; a half pair cannot reach this
  schema.  Tying them here is not free: the contract diff has no rule for
  ``dependentRequired`` and reports it as a breaking change.
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

from .wire import (
    AMBIGUOUS_SPACE_DATA,
    CREATE_ERRORS,
    UNAVAILABLE_DATA,
    UNKNOWN_SPACE_DATA,
)

JsonObject = dict[str, Any]

_ORG = {"type": "string", "minLength": 1}
_STRING = {"type": "string"}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer"}

_NETWORK_PARAMS = {
    "type": "object",
    "properties": {"org": _ORG, "peerKey": _ORG},
    "additionalProperties": False,
}

_REPAIR_PARAMS = {
    "type": "object",
    "properties": {"org": _ORG, "apply": _BOOL},
    "additionalProperties": False,
}

_REPAIR_CANDIDATE = {
    "type": "object",
    "properties": {
        "org": _ORG,
        "spaceId": _ORG,
        "reason": _STRING,
        "repairable": _BOOL,
        "parent": {"type": ["string", "null"]},
        "parentSpaceId": {"type": ["string", "null"]},
        "tailnetId": {"type": ["string", "null"]},
        "error": _STRING,
    },
    "required": ["org", "spaceId", "reason", "repairable"],
    "additionalProperties": False,
}

_SPACE_JSON = {
    "type": "object",
    "properties": {
        "spaceId": _ORG,
        "name": _ORG,
        "owner": _ORG,
        "createdAt": _ORG,
    },
    "required": ["spaceId", "name", "owner", "createdAt"],
    "additionalProperties": False,
}

_CREATE_RESULT = {
    "type": "object",
    "properties": {
        "org": _ORG,
        "spaceId": _ORG,
        "aclSpaceId": _ORG,
        "space": _SPACE_JSON,
        "aclSpace": _SPACE_JSON,
        "existed": _BOOL,
        "aclExisted": _BOOL,
        "casdoorGroup": {"type": "string", "enum": ["not-configured", "verified"]},
        "tailnetInitialized": _BOOL,
        "published": _BOOL,
        "devicePublished": _BOOL,
        "parent": {"type": ["string", "null"]},
        "parentSpaceId": _ORG,
        "tailnetId": _ORG,
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
        "published",
        "devicePublished",
    ],
    "additionalProperties": False,
}

_REPAIR_RESULT = {
    "type": "object",
    "properties": {
        "apply": _BOOL,
        "candidates": {"type": "array", "items": _REPAIR_CANDIDATE},
        "repaired": {"type": "array", "items": _CREATE_RESULT},
    },
    "required": ["apply", "candidates", "repaired"],
    "additionalProperties": False,
}

_PUBLISH_RESULT = {
    "type": "object",
    "properties": {"published": _BOOL, "orgfsPublished": _BOOL},
    "required": ["published", "orgfsPublished"],
    "additionalProperties": False,
}

_PANE_STATUS = {
    "type": "object",
    "properties": {
        "available": _BOOL,
        "reason": {
            "type": "string",
            "enum": ["timeout", "capacity", "not-configured", "error"],
        },
        "errorType": _STRING,
    },
    "required": ["available"],
    "additionalProperties": False,
}

_DEVICE_VIEW = {
    "type": "object",
    "properties": {
        "deviceId": _ORG,
        "owner": _ORG,
        "serverPublic": _ORG,
        "clientPublic": _ORG,
        "clientPublicByServer": {
            "type": "object",
            "additionalProperties": {"type": "string", "minLength": 1},
        },
        "addressPresent": _BOOL,
        "keyGeneration": {"type": "integer", "minimum": 1},
        "updatedAt": _ORG,
        "presence": _STRING,
        "machineFingerprint": _STRING,
        "firstPublishedAt": _STRING,
        # UUID addressing: present only when the publisher stamped it.
        "deviceUid": {"type": ["string", "null"]},
        "encryptionKeyId": _ORG,
        "encryptionPublicKey": _ORG,
    },
    "required": [
        "deviceId",
        "owner",
        "serverPublic",
        "clientPublic",
        "clientPublicByServer",
        "addressPresent",
        "keyGeneration",
        "updatedAt",
        "presence",
    ],
    "additionalProperties": False,
}

_DATA_PROBLEM = {
    "type": "object",
    "properties": {
        "org": _ORG,
        "path": _ORG,
        "reason": _ORG,
        "value": _STRING,
        "owner": _STRING,
        "deviceId": _STRING,
        # OrgFS node versions are opaque frontier strings, not integers.
        "version": _STRING,
    },
    "required": ["org", "path", "reason"],
    "additionalProperties": False,
}

_UNAVAILABLE_ORG = {
    "type": "object",
    "properties": {
        "org": _ORG,
        "available": {"const": False},
        "reason": {"type": "string", "enum": ["creating", "partial", "policy-denied"]},
    },
    "required": ["org", "available", "reason"],
    "additionalProperties": False,
}

_CONTENT_PENDING = {
    "type": "object",
    "properties": {
        "owner": _STRING,
        "deviceId": _STRING,
        "reason": _STRING,
    },
    "required": ["owner", "deviceId", "reason"],
    "additionalProperties": False,
}

_DIRECTORY_PENDING = {
    "type": "object",
    "properties": {
        "contentPending": {"type": "array", "items": _CONTENT_PENDING},
        "dataProblems": {"type": "array", "items": _DATA_PROBLEM},
    },
    "required": ["contentPending"],
    "additionalProperties": False,
}

_LAST_POLL = {
    "type": "object",
    "properties": {
        "atMs": {"type": ["integer", "null"]},
        "ok": {"type": ["boolean", "null"]},
        "error": {"type": ["string", "null"]},
        "peersReported": {"type": ["boolean", "null"]},
        "peerCount": _INT,
        "derpMapURL": {"type": ["string", "null"]},
        "derpMapSource": {"type": ["string", "null"]},
    },
    "additionalProperties": False,
}

_SLOTS = {
    "type": "object",
    "properties": {
        "poolSize": _INT,
        "free": _INT,
        "assigned": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slot": {"type": ["integer", "string"]},
                    "peer": _STRING,
                    "endpoint": {"type": ["string", "null"]},
                },
                "required": ["slot", "peer"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["poolSize", "free", "assigned"],
    "additionalProperties": False,
}

_SIDECAR = {
    "type": ["object", "null"],
    "properties": {
        "policy": {
            "type": "object",
            "properties": {"mode": _STRING, "source": _STRING},
            "required": ["mode", "source"],
            "additionalProperties": False,
        },
        "endpoints": {"type": ["array", "null"], "items": {"type": "string"}},
        "dialed": {"type": ["array", "null"], "items": {"type": "string"}},
        "restartRequired": {"type": ["boolean", "null"]},
        "lastPoll": {"oneOf": [_LAST_POLL, {"type": "null"}]},
        "slots": {"oneOf": [_SLOTS, {"type": "null"}]},
        "connectionTelemetry": {"type": ["object", "null"]},
        "derpMapURL": {"type": ["string", "null"]},
        "derpMapSource": {"type": ["string", "null"]},
    },
    "additionalProperties": False,
}

_MAPPED_PEER = {
    "type": "object",
    "properties": {
        "peer": _STRING,
        "endpoint": {"type": ["string", "null"]},
        "dialed": _BOOL,
        "slot": {"type": ["integer", "string"]},
    },
    "required": ["peer", "endpoint", "dialed"],
    "additionalProperties": False,
}

_CASDOOR_STATUS = {
    "type": "object",
    "properties": {
        "casdoorOnly": {"type": "array", "items": _STRING},
        "casdoorMissing": {"type": "array", "items": _STRING},
        "lastSyncAt": {"type": ["string", "null"]},
        "lastFullSyncAt": {"type": ["string", "null"]},
        "lastError": {"type": ["object", "null"]},
        "syncHalted": {"type": ["object", "null"]},
    },
    "additionalProperties": False,
}

_NETWORK_RESULT = {
    "type": "object",
    "properties": {
        "owner": {"type": ["string", "null"]},
        "orgs": {"type": "array", "items": _ORG},
        "unavailableOrgs": {"type": "array", "items": _UNAVAILABLE_ORG},
        "dataProblems": {"type": "array", "items": _DATA_PROBLEM},
        "directory": {
            "type": "object",
            "additionalProperties": {"type": "array", "items": _DEVICE_VIEW},
        },
        "directoryPending": {
            "type": "object",
            "additionalProperties": {
                "oneOf": [_DIRECTORY_PENDING, {"type": "null"}]
            },
        },
        "self": {
            "type": "object",
            "properties": {
                "deviceId": _ORG,
                # Null until this device has minted its UUID identity.
                "deviceUid": {"type": ["string", "null"]},
                "owner": _ORG,
                "serverPublic": _ORG,
                "addressPresent": _BOOL,
            },
            "required": ["deviceId", "owner", "serverPublic", "addressPresent"],
            "additionalProperties": False,
        },
        "mappedPeers": {
            "type": ["array", "null"],
            "items": _MAPPED_PEER,
        },
        "sidecar": _SIDECAR,
        "paneStatus": {
            "type": "object",
            "properties": {
                "directory": _PANE_STATUS,
                "mappedPeers": _PANE_STATUS,
                "sidecar": _PANE_STATUS,
            },
            "required": ["directory", "mappedPeers", "sidecar"],
            "additionalProperties": False,
        },
        "sources": {
            "type": "object",
            "properties": {
                "directory": _STRING,
                "mappedPeers": _STRING,
                "sidecar": _STRING,
            },
            "required": ["directory", "mappedPeers", "sidecar"],
            "additionalProperties": False,
        },
        "casdoorSync": {
            "type": ["object", "null"],
            "additionalProperties": _CASDOOR_STATUS,
        },
    },
    "anyOf": [
        {
            "type": "object",
            "required": [
                "orgs",
                "unavailableOrgs",
                "dataProblems",
                "directory",
                "directoryPending",
                "self",
                "mappedPeers",
                "sidecar",
                "paneStatus",
                "sources",
            ],
        },
        {"type": "object", "required": ["owner"]},
    ],
    "additionalProperties": False,
}


def _error(
    code: str, data: Mapping[str, Any] | None = None, description: str = ""
) -> MethodError:
    return MethodError(code, data, description)


_COMMON_READ_ERRORS = (
    _error(ipc_errors.INVALID_ARGUMENT, description="no data"),
    _error("ORG_ACTION_NOT_AUTHORIZED", description="the owner policy denies this read"),
    _error(
        "ambiguous-space",
        AMBIGUOUS_SPACE_DATA,
        "more than one space carries the org name",
    ),
    _error("unknown-space", UNKNOWN_SPACE_DATA, "the org space disappeared"),
    _error(
        "unavailable",
        UNAVAILABLE_DATA,
        "orgfs is closing or an authority is unavailable",
    ),
    _error("ORG_NETWORK_ERROR", description="an unexpected org-network failure"),
)

_NETWORK_ERRORS = _COMMON_READ_ERRORS + (
    _error("MEMBERSHIP_NOT_INITIALIZED", description="the tailnet is not initialized"),
    _error(
        ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT,
        description="the sidecar missed the request budget",
    ),
    _error("DEVICE_RECORD_MISSING", description="no local device record exists"),
    _error("FORWARDING_UNAVAILABLE", description="forwarding is not configured"),
)

_REPAIR_ERRORS = CREATE_ERRORS + (
    _error(ipc_errors.DAEMON_NOT_READY, description="org service is not wired"),
)

_PUBLISH_ERRORS = (
    _error(ipc_errors.INVALID_ARGUMENT, description="no data"),
    _error(
        ipc_errors.DAEMON_NOT_READY,
        description="the org-context endpoint is not ready",
    ),
    _error("ORG_NETWORK_ERROR", description="an unexpected org-network failure"),
)


def org_network(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_network

    return ipc_org_network(app, dict(params))


def org_repair(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_repair

    return ipc_org_repair(app, dict(params))


def org_publish(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    handler = getattr(app, "_ipc_org_publish", None)
    if handler is None:
        raise DaemonRequestError(
            ipc_errors.DAEMON_NOT_READY, "org-context publish is not wired"
        )
    return handler()


ORG_NETWORK_SPEC = MethodSpec(
    name="org.network",
    owner="daemon.org",
    params_schema=_NETWORK_PARAMS,
    result_schema=_NETWORK_RESULT,
    errors=_NETWORK_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator",),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="org-network-read"),
)

ORG_REPAIR_SPEC = MethodSpec(
    name="org.repair",
    owner="daemon.org",
    params_schema=_REPAIR_PARAMS,
    result_schema=_REPAIR_RESULT,
    errors=_REPAIR_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="org-repair"),
)

ORG_PUBLISH_SPEC = MethodSpec(
    name="org.publish",
    owner="daemon.org",
    params_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    result_schema=_PUBLISH_RESULT,
    errors=_PUBLISH_ERRORS,
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=True,
    http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="org-publish"),
)

BATCH2_ORG_BINDINGS = (
    (ORG_NETWORK_SPEC, org_network),
    (ORG_PUBLISH_SPEC, org_publish),
    (ORG_REPAIR_SPEC, org_repair),
)

__all__ = [
    "BATCH2_ORG_BINDINGS",
    "ORG_NETWORK_SPEC",
    "ORG_PUBLISH_SPEC",
    "ORG_REPAIR_SPEC",
    "org_network",
    "org_publish",
    "org_repair",
]
