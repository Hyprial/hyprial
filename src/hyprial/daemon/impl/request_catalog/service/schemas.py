"""Schemas for the service manager's actual response shapes."""


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


STR = {"type": "string"}
NSTR = {"type": ["string", "null"]}
NINT = {"type": ["integer", "null"]}
ENTRY = obj({
    "name": STR, "deviceId": STR, "remotePort": {"type": "integer"},
    "protocol": STR, "localPort": NINT, "usage": NSTR,
    "env": {"type": "object", "additionalProperties": STR}, "auth": STR, "guide": STR,
})
ROW = obj({
    "name": STR, "deviceId": NSTR, "remotePort": NINT, "recordGeneration": NINT,
    "localPort": NINT, "activeConnections": {"type": "integer", "minimum": 0},
    "path": {"enum": ["direct", "relay", "unknown"]},
    "pathDetail": NSTR, "observedAtMs": {"type": ["integer", "null"], "minimum": 0},
    "lastDialState": {"enum": ["unknown", "ok", "error"]},
    "lastDialMs": {"type": ["number", "null"], "minimum": 0}, "lastError": NSTR,
    "lastErrorAtMs": {"type": ["integer", "null"], "minimum": 0},
    "state": {"enum": ["failed", "listening"]}, "errorCode": NSTR,
})
REGISTRY = obj({"source": STR, "spaceId": NSTR, "owner": NSTR, "error": NSTR})
GRANT_ENDPOINT = obj({
    "deviceId": STR,
    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
    "name": NSTR,
    "protocol": {"enum": ["tcp", "http"]},
    "note": NSTR,
})
GRANT_RECIPIENT = {
    "anyOf": [
        {"const": "@members"},
        obj({"owner": STR, "deviceId": STR}),
    ]
}
TOMBSTONE_RECIPIENT = {"anyOf": [{"type": "null"}, *GRANT_RECIPIENT["anyOf"]]}
GRANT_VIEW = obj({
    "org": STR,
    "grantor": STR,
    "schemaVersion": {"type": "integer", "const": 1},
    "grantId": STR,
    "service": {"anyOf": [{"type": "null"}, GRANT_ENDPOINT]},
    "recipient": TOMBSTONE_RECIPIENT,
    "grantedAtMs": {"type": ["integer", "null"], "minimum": 0},
    "expiresAtMs": {"type": ["integer", "null"], "minimum": 0},
    "revokedAtMs": {"type": ["integer", "null"], "minimum": 0},
    "hasCredential": {"type": "boolean"},
    "effective": {"type": "boolean"},
})
CREDENTIAL_REF = {
    "anyOf": [
        {"type": "null"},
        {
            "type": "object",
            "properties": {
                "entryId": {"type": "string", "minLength": 1},
                "fieldName": {"type": ["string", "null"], "minLength": 1},
            },
            "required": ["entryId"],
            "additionalProperties": False,
        },
    ]
}
GRANT_PARAMS = {
    "service.grant": {
        "type": "object",
        "properties": {
            "org": {"type": "string", "minLength": 1},
            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
            "device": {"type": "string", "minLength": 1},
            "expiresAtMs": {"type": ["integer", "null"], "minimum": 0},
            "name": {"type": ["string", "null"], "minLength": 1},
            "protocol": {"enum": ["tcp", "http", None]},
            "note": NSTR,
            "credential": CREDENTIAL_REF,
        },
        "required": ["org", "port", "device"],
        "additionalProperties": False,
    },
    "service.revoke": {
        "type": "object",
        "properties": {
            "org": {"type": "string", "minLength": 1},
            "grantId": {"type": "string", "minLength": 1},
        },
        "required": ["org", "grantId"],
        "additionalProperties": False,
    },
    "service.grants": {
        "type": "object",
        "properties": {
            "org": {"type": ["string", "null"], "minLength": 1},
            "port": {"type": ["integer", "null"], "minimum": 1, "maximum": 65535},
            "includeRevoked": {"type": "boolean"},
        },
        "required": [],
        "additionalProperties": False,
    },
}
RESULTS = {
    "service.connect": obj({"service": ROW, "env": {"type": "object", "additionalProperties": STR}, "auth": STR, "guide": STR}),
    "service.disconnect": obj({"name": STR, "removed": {"type": "boolean"}}),
    "service.guide": obj({"entry": ENTRY, "source": STR}),
    "service.list": obj({"services": {"type": "array", "items": ROW}, "registry": REGISTRY}),
    "service.grant": obj({"grant": GRANT_VIEW, "superseded": {"type": "array", "items": STR}}),
    "service.revoke": obj({"grantId": STR, "revoked": {"type": "boolean"}}),
    "service.grants": obj({
        "granted": {"type": "array", "items": GRANT_VIEW},
        "received": {"type": "array", "items": GRANT_VIEW},
    }),
}
