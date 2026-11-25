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
RESULTS = {
    "service.connect": obj({"service": ROW, "env": {"type": "object", "additionalProperties": STR}, "auth": STR, "guide": STR}),
    "service.disconnect": obj({"name": STR, "removed": {"type": "boolean"}}),
    "service.guide": obj({"entry": ENTRY, "source": STR}),
    "service.list": obj({"services": {"type": "array", "items": ROW}, "registry": REGISTRY}),
}
