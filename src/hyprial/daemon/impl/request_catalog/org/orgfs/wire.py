"""Shared JSON schemas and error vocabulary for the orgfs.* MethodSpecs.

The orgfs runtime exposes one closed error vocabulary (``ORGFS_ERROR_CODES``)
plus the daemon-owned path/ACL refusal codes.  Individual methods can raise a
subset of it; the subsets below group them by operation shape rather than
copying a different 30-entry tuple into every declaration.
"""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.orgfs.api import ORGFS_ERROR_CODES
from hyprial.kernel import MethodError, ipc_errors

String = {"type": "string"}
Boolean = {"type": "boolean"}
Integer = {"type": "integer"}
Number = {"type": "number"}

#: MCP's ``StatelessDaemonProxy`` appends the daemon-verified session identity
#: to every call.  These are transport credentials, not orgfs business params:
#: IPC/MCP handlers consume them through the existing session/org-context
#: fence, while the HTTP entry must refuse them (the server honours the
#: ``x-hyprial-http-forbidden`` marker).  A browser may not self-report an
#: actor or a session reference just because the daemon-side schema admits it.
SESSION_PROPERTIES: dict[str, Any] = {
    "actor": {
        "type": "string",
        "minLength": 1,
        "x-hyprial-http-forbidden": True,
    },
    "sessionRef": {
        "type": "string",
        "minLength": 1,
        "x-hyprial-http-forbidden": True,
    },
    # The CLI's owner/worker claim.  The handler already prefers ``actor`` and
    # falls back to ``from``; keeping it here restores the pre-registry wire
    # without letting an HTTP browser choose it.
    "from": {
        "type": "string",
        "minLength": 1,
        "x-hyprial-http-forbidden": True,
    },
}

#: The org-context refusal carries the verified caller, capability, entry
#: point, resource and reason.  ``ORG_CONTEXT_NOT_AUTHORIZED`` is raised only
#: by the six orgfs reads in ``_ORG_CONTEXT_READ_METHODS``; declaring it for
#: every orgfs method would claim an error those handlers cannot produce.
ORG_CONTEXT_DATA = {
    "type": "object",
    "properties": {
        "caller": String,
        "capability": String,
        "entryPoint": String,
        "resource": String,
        "reason": String,
    },
    "required": ["caller", "capability", "entryPoint", "resource", "reason"],
    "additionalProperties": False,
}

SESSION_ERRORS = (
    MethodError(
        ipc_errors.STALE_SESSION,
        description="the presented session no longer owns the actor",
    ),
    MethodError(
        ipc_errors.SESSION_SUPERSEDED,
        description="a newer session now owns the actor",
    ),
)

ORG_CONTEXT_ERRORS = (
    MethodError(
        ipc_errors.ORG_CONTEXT_NOT_AUTHORIZED,
        ORG_CONTEXT_DATA,
        description="the caller has no org-context grant for the accepted document",
    ),
    *SESSION_ERRORS,
)

#: Error data is an object owned by the producer.  The contract freezes the
#: error code; it deliberately does not freeze every diagnostic field, because
#: one code (for example ``invalid-argument``) carries different fields from
#: different orgfs operations.
_ERROR_VALUE = {
    "type": ["string", "number", "boolean", "null", "array", "object"],
}
ERROR_DATA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": _ERROR_VALUE,
}


def node_schema(content_state_key: str = "contentState") -> dict[str, Any]:
    """One orgfs node as projected by ``_orgfs_json``.

    ``read``/``ls``/``stat`` receive a dataclass directly and therefore name the
    field ``contentState``; histories embed the node through ``asdict`` and
    keep the dataclass spelling ``content_state``.  Both spellings are real.
    """

    return {
        "type": "object",
        "properties": {
            "space_id": String,
            "node_id": String,
            "kind": {"type": "string", "enum": ["dir", "doc", "blob"]},
            "name": String,
            "path": String,
            "size": {"type": ["integer", "null"]},
            "blob_hash": {"type": ["string", "null"]},
            "doc_id": {"type": ["string", "null"]},
            "version": String,
            "modified_by": String,
            "modified_via": {"type": ["string", "null"]},
            "name_conflict": Boolean,
            "deleted": Boolean,
            content_state_key: {
                "type": ["string", "null"],
                "enum": ["arrived", "pending", "unverifiable", None],
            },
            "uri": String,
        },
        "required": [
            "space_id",
            "node_id",
            "kind",
            "name",
            "path",
            "size",
            "blob_hash",
            "doc_id",
            "version",
            "modified_by",
            "modified_via",
            "name_conflict",
            "deleted",
            content_state_key,
            "uri",
        ],
        "additionalProperties": False,
    }


NODE = node_schema()
HISTORY_NODE = node_schema("content_state")

SPACE = {
    "type": "object",
    "properties": {
        "space_id": String,
        "name": String,
        "owner": String,
        "created_at": String,
    },
    "required": ["space_id", "name", "owner", "created_at"],
    "additionalProperties": False,
}

MEMBER = {
    "type": "object",
    "properties": {
        "space_id": String,
        "user": String,
        "mode": {"type": "string", "enum": ["ro", "rw"]},
        "added_by": String,
        "added_at": String,
    },
    "required": ["space_id", "user", "mode", "added_by", "added_at"],
    "additionalProperties": False,
}

HISTORY_ENTRY = {
    "type": "object",
    "properties": {
        "node": HISTORY_NODE,
        "changed": {
            "type": "string",
            "enum": ["position", "content", "both", "created", "removed", "restored"],
        },
        "author": String,
        "actor": {"type": ["string", "null"]},
        "at": String,
    },
    "required": ["node", "changed", "author", "actor", "at"],
    "additionalProperties": False,
}

SPACE_STATUS = {
    "type": "object",
    "properties": {
        "space_id": String,
        "unconfirmed_commits": Integer,
        "durable_holders_seen": {"type": "array", "items": String},
        "holders_online": {"type": "array", "items": String},
        "parked_commits": {
            "type": "array",
            "items": {
                "type": "array",
                "items": {"type": ["string", "integer"]},
                "minItems": 2,
                "maxItems": 2,
            },
        },
    },
    "required": [
        "space_id",
        "unconfirmed_commits",
        "durable_holders_seen",
        "holders_online",
        "parked_commits",
    ],
    "additionalProperties": False,
}

CHANGE_EVENT = {
    "type": "object",
    "properties": {
        "spaceId": String,
        "kind": {
            "type": "string",
            "enum": ["created", "modified", "moved", "removed", "restored"],
        },
        "node": NODE,
        "oldPath": {"type": ["string", "null"]},
    },
    "required": ["spaceId", "kind", "node", "oldPath"],
    "additionalProperties": False,
}

PURGE_STATUS = {
    "type": "object",
    "properties": {
        # ``_orgfs_json`` serialises the dataclass with ``asdict``; the real
        # PurgeStatus therefore keeps the snake_case field name.  ``planId``
        # is only the request parameter spelling.
        "plan_id": String,
        "acknowledged": {"type": "array", "items": String},
        "pending": {"type": "array", "items": String},
    },
    "required": ["plan_id", "acknowledged", "pending"],
    "additionalProperties": False,
}

_READ_CODES = frozenset(
    {
        "ambiguous-path",
        "ambiguous-space",
        "blob-unavailable",
        "closed",
        "log-key-conflict",
        "no-holder-online",
        "not-a-member",
        "not-owner",
        "out-of-range",
        "protected-path",
        "purged",
        "resource-exhausted",
        "snapshot-barrier",
        "stale-write",
        "too-large",
        "unavailable",
        "unknown-blob",
        "unknown-doc",
        "unknown-space",
        "unsafe-space-path",
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.ORGFS_CONTENT_PENDING,
        ipc_errors.ORGFS_CROSS_SPACE_URI,
        ipc_errors.ORGFS_INVALID_URI,
    }
)

_WRITE_EXTRA = frozenset(
    {
        "checkout-io",
        "stale-plan",
        "space-purge-incomplete",
        "space-purge-refused",
        ipc_errors.ORGFS_ACL_SPACE_EXTERNAL_MUTATION,
        ipc_errors.ORG_ACL_WRITE_DENIED,
    }
)

_PATH_EXTRA = frozenset({ipc_errors.ATTACHMENT_PATH_REFUSED})

#: The producer set is the source of truth; keep only codes the registry can
#: declare (the frozen kebab/constant vocabulary).  Unknown additions fail the
#: first MethodSpec construction instead of silently disappearing.
_UNKNOWN = _READ_CODES | _WRITE_EXTRA | _PATH_EXTRA
if not _UNKNOWN <= ORGFS_ERROR_CODES | {
    ipc_errors.INVALID_ARGUMENT,
    ipc_errors.ORGFS_ACL_SPACE_EXTERNAL_MUTATION,
    ipc_errors.ORG_ACL_WRITE_DENIED,
    ipc_errors.ATTACHMENT_PATH_REFUSED,
}:
    raise RuntimeError(
        "orgfs error group names an undeclared code: "
        + ", ".join(sorted(_UNKNOWN - ORGFS_ERROR_CODES))
    )


def _errors(codes: frozenset[str]) -> tuple[MethodError, ...]:
    return tuple(
        MethodError(code, ERROR_DATA, description="orgfs runtime error")
        for code in sorted(codes)
    )


READ_ERRORS = _errors(_READ_CODES)
PURGE_STATUS_ERRORS = _errors(_READ_CODES | {"stale-plan"})
WRITE_ERRORS = _errors(_READ_CODES | _WRITE_EXTRA)
PATH_ERRORS = _errors(_READ_CODES | _WRITE_EXTRA | _PATH_EXTRA)
PURGE_ERRORS = _errors(_READ_CODES | _WRITE_EXTRA)
