"""Wire shapes of the existing Agent registry and home-domain producers."""

from typing import Any

STRING = {"type": "string"}
NAME = {"type": "string", "minLength": 1}
OPTIONAL_STRING = {"type": ["string", "null"]}
BOOL = {"type": "boolean"}
STRINGS = {"type": "array", "items": STRING}
JSON_VALUE = {"type": ["object", "array", "string", "number", "boolean", "null"]}


def record(
    properties: dict[str, Any], *, optional: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": [key for key in properties if key not in optional],
        "additionalProperties": False,
    }


def params(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    # The native handlers ignore unknown keys and own validation/error order.
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": JSON_VALUE,
    }


NAME_PARAMS = params({"name": NAME}, "name")
CONTEXT_PARAMS = params(
    {
        "name": NAME,
        "harness": NAME,
        "cwd": OPTIONAL_STRING,
        "modelProvider": OPTIONAL_STRING,
        "credentialPreflight": BOOL,
    },
    "name",
    "harness",
)
READINESS_PARAMS = params(
    {
        "name": NAME,
        "role": NAME,
        "harness": OPTIONAL_STRING,
        "cwd": OPTIONAL_STRING,
        "tools": {"type": ["array", "null"], "items": NAME},
        "workPaths": {"type": ["array", "null"], "items": NAME},
        "expectLogin": OPTIONAL_STRING,
        "forgejoHost": OPTIONAL_STRING,
        "forgejoIdentity": BOOL,
        "timeoutSeconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
    },
    "name",
    "role",
)

READINESS_RESULT = record(
    {
        "ok": {"const": True},
        "ready": BOOL,
        "status": {"enum": ["ready", "not-ready"]},
        "actor": STRING,
        "harness": STRING,
        "role": STRING,
        "mode": STRING,
        "modeReason": OPTIONAL_STRING,
        "running": BOOL,
        "configRevision": OPTIONAL_STRING,
        "publishedRevisions": STRINGS,
        "reloadRequired": {"type": ["boolean", "null"]},
        "identity": OPTIONAL_STRING,
        "checks": {
            "type": "array",
            "items": record(
                {
                    "name": STRING,
                    "status": {"enum": ["pass", "blocked", "unknown"]},
                    "detail": STRING,
                    "action": STRING,
                },
                optional=("action",),
            ),
        },
        "limits": STRINGS,
    }
)
ENSURE_RESULT = record(
    {
        "ok": {"const": True},
        "actor": NAME,
        "agent": NAME,
        "home": record(
            {
                "schemaVersion": {"const": 1},
                "actor": NAME,
                "entityToken": NAME,
                "resourceToken": NAME,
                "path": NAME,
                "ownerUid": {"type": "integer"},
                "status": {"const": "ready"},
            }
        ),
    }
)

_ENVIRONMENT = {"type": "object", "additionalProperties": STRING}
_COMMON = {
    "ok": {"const": True},
    "mode": {"enum": ["legacy", "agent-home-p2"]},
    "environment": _ENVIRONMENT,
}
_P2 = {
    "authorityPrepared": BOOL,
    "launchToken": NAME,
    "actor": NAME,
    "harness": NAME,
    "configRevision": STRING,
    "mappingVersion": STRING,
    "projectionRoot": NAME,
    "nativeRoot": NAME,
    "sessionRoot": NAME,
    "toolProfileId": NAME,
    "notices": {
        "type": "array",
        "items": record(
            {
                "code": STRING,
                "actor": STRING,
                "message": STRING,
                "path": STRING,
            }
        ),
    },
    "auth": {
        "anyOf": [{"type": "null"}, record({"method": STRING, "revision": STRING})]
    },
    "sharedCredential": {
        "anyOf": [
            {"type": "null"},
            record(
                {
                    "authMode": {"const": "native-shared-link"},
                    "nativePath": NAME,
                    "targetPath": NAME,
                }
            ),
        ]
    },
}
CONTEXT_RESULT = {
    **record({**_COMMON, **_P2}, optional=tuple(_P2)),
    "oneOf": [
        {
            "type": "object",
            "maxProperties": len(_COMMON),
            "properties": {
                "mode": {"const": "legacy"},
                "environment": {"type": "object", "maxProperties": 0},
            },
        },
        {
            "type": "object",
            "properties": {"mode": {"const": "agent-home-p2"}},
            "required": [key for key in _P2 if key != "launchToken"],
        },
    ],
}


CREDENTIAL_ERROR_DATA = record(
    {
        "reason": STRING,
        "path": STRING,
        "remediation": {"type": "object", "additionalProperties": JSON_VALUE},
    }
)
