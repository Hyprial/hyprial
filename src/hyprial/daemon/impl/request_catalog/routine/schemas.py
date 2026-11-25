"""Routine request shapes derived from the existing service projections."""

from hyprial.daemon.impl.request_catalog.agent.relations import _ROUTINE_ROW

ANY_JSON = {"type": ["string", "number", "boolean", "null", "array", "object"]}
STRING = {"type": "string"}


def obj(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


ENABLED = obj({"name": STRING, "enabled": {"type": "boolean"}}, ["name", "enabled"])
AUDIT_ROW = {**_ROUTINE_ROW, "required": [x for x in _ROUTINE_ROW["required"]
                                           if x not in {"lastDispatchAtMs", "lastOutcome"}]}
MIGRATION = obj({"routine": STRING, "field": STRING, "before": STRING,
                 "after": STRING, "migratedAtMs": {"type": "integer"}},
                ["routine", "field", "before", "after", "migratedAtMs"])
AUDIT = obj({"quarantined": {"type": "array", "items": AUDIT_ROW},
             "addressMigrations": {"type": "array", "items": MIGRATION}},
            ["quarantined", "addressMigrations"])
RESULTS = {"routine.audit": AUDIT, "routine.pause": ENABLED, "routine.resume": ENABLED}


def params_schema(name):
    properties = {"actor": ANY_JSON, "sessionRef": ANY_JSON}
    required = []
    if name in {"routine.pause", "routine.resume"}:
        properties["name"] = {"type": "string", "minLength": 1}
        required.append("name")
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": ANY_JSON}
