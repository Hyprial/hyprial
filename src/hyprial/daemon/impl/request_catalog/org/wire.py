"""Shared JSON schemas for the org MethodSpec wire shapes.

The package initializer imports the per-lifecycle spec modules, so those
modules cannot import the initializer back.  Schemas that several spec
families share live here instead of being copied into each module.
"""

from __future__ import annotations

from typing import Any

from hyprial.kernel import MethodError, ipc_errors

EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

#: Real producers can attach one of these shapes to their error envelope.
AMBIGUOUS_SPACE_DATA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "org": {"type": "string"},
        "space": {"type": "string"},
        "count": {"type": "integer", "minimum": 1},
    },
    "additionalProperties": False,
    "anyOf": [
        {"type": "object", "required": ["space", "count"]},
        {"type": "object", "required": ["org"]},
    ],
}

UNKNOWN_SPACE_DATA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "org": {"type": "string"},
        "space": {"type": "string"},
        "spaceId": {"type": "string"},
        "reason": {"type": "string"},
    },
    "additionalProperties": False,
    "anyOf": [
        {"type": "object", "required": ["spaceId"]},
        {"type": "object", "required": ["org", "space"]},
        {"type": "object", "required": ["org", "reason"]},
    ],
}

UNAVAILABLE_DATA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "spaceId": {"type": "string"},
        "leg": {"type": "string"},
        "budgetSeconds": {"type": "number"},
    },
    "additionalProperties": False,
}

#: A real producer can attach an empty data object (for example OrgFsError
#: consumes its internal ``message`` key before the IPC boundary).
EMPTY_ERROR_DATA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

#: ``org.create`` and ``org.repair`` share one error surface: repair resumes
#: the same create path, so it can raise every create error.
CREATE_ERRORS = (
    MethodError(ipc_errors.INVALID_ARGUMENT, description="no data"),
    MethodError(
        "invalid-argument",
        EMPTY_ERROR_DATA,
        description=(
            "org metadata or a create-path argument is not valid; the wire "
            "data is an empty object"
        ),
    ),
    MethodError(
        "ORG_ACTION_NOT_AUTHORIZED",
        description="this owner may not resume that create intent or create here",
    ),
    MethodError(
        "ambiguous-space",
        AMBIGUOUS_SPACE_DATA,
        description="more than one space carries the org or its ACL name",
    ),
    MethodError(
        "unknown-space",
        UNKNOWN_SPACE_DATA,
        description="a space created for this org disappeared before the write",
    ),
    MethodError(
        "unavailable",
        UNAVAILABLE_DATA,
        description="orgfs is closing while the create commit is running",
    ),
    MethodError(
        "DEVICE_RECORD_MISSING",
        description="no device record exists to seed the new tailnet",
    ),
    MethodError(
        "AUTHORITY_UNREACHABLE",
        description="the new tailnet has no local Tailcat address to seed",
    ),
    MethodError(
        "MEMBERSHIP_NOT_INITIALIZED",
        description="the membership sidecar or its store is not initialized",
    ),
    MethodError(
        "MEMBERSHIP_STORE_INVALID",
        description="the membership store cannot be read",
    ),
    MethodError(
        ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT,
        description="tailnet initialization exceeded the request-path sidecar budget",
    ),
    MethodError(
        "BAD_REQUEST",
        description="the forwarding tailnet settings are invalid",
    ),
    MethodError(
        "CASDOOR_GROUP_UNAVAILABLE",
        description="the organization group cannot be ensured in Casdoor",
    ),
    MethodError(
        "CASDOOR_ORG_QUOTA_EXCEEDED",
        description="the owner's Casdoor organization quota is exhausted",
    ),
    MethodError(
        ipc_errors.ORG_ALREADY_DELETED,
        description="the org name was deleted on this home and cannot be resurrected",
    ),
)
