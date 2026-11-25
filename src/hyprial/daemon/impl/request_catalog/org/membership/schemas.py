"""Shared schema fragments for the ``org.*`` membership declarations."""

from __future__ import annotations

NOTICES = {"type": "array", "items": {"type": "string"}}
SELECTION_PARAMS = {
    "type": "object",
    "properties": {"org": {"type": "string", "minLength": 1}, "spaceId": {"type": "string", "minLength": 1}},
    "additionalProperties": False,
}
SELECTION_ERROR = {
    "type": "object",
    "properties": {"candidates": NOTICES, "notices": NOTICES},
    "additionalProperties": False,
}
RESOLVE_RESULT = {
    "type": "object",
    "properties": {"ok": {"const": True}, "org": {"type": ["string", "null"]}, "spaceId": {"type": "string"}, "notices": NOTICES},
    "required": ["ok", "org", "spaceId", "notices"],
    "additionalProperties": False,
}
USE_PARAMS = {
    "type": "object",
    "properties": {"org": {"type": "string", "minLength": 1}, "clear": {"const": True}},
    "additionalProperties": False,
}
USE_RESULT = {
    "type": "object",
    "properties": {"ok": {"const": True}, "current": {"type": ["string", "null"]}, "notices": NOTICES},
    "required": ["ok", "current", "notices"],
    "additionalProperties": False,
}
LIST_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True}, "notices": NOTICES, "leftOrgs": NOTICES,
        "orgs": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "org": {"type": "string"}, "spaceId": {"type": ["string", "null"]},
                "role": {"type": ["string", "null"]}, "devices": {"type": "integer"},
                "state": {"type": "string"}, "kind": {"enum": ["work"]},
                "current": {"type": "boolean"},
                "authorityDevice": {"type": ["boolean", "null"]},
                "parent": {"type": "string", "minLength": 1},
                "tailnetId": {"type": "string", "minLength": 1},
            },
            "required": ["org", "spaceId", "role", "devices", "state", "kind", "current", "authorityDevice"],
            "additionalProperties": False,
        }},
    },
    "required": ["ok", "orgs", "leftOrgs", "notices"],
    "additionalProperties": False,
}
