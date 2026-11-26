"""Closed request, result and classification shapes for model configuration."""

from hyprial.error import codes
from hyprial.kernel import MethodError

TEXT = {"type": "string", "minLength": 1, "maxLength": 512}
REVISION = {"type": "integer", "minimum": 0, "maximum": 9007199254740991}
POSITIVE = {**REVISION, "minimum": 1}
NULL_REVISION = {**REVISION, "type": ["integer", "null"]}


def obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


CONNECTION_CONFIGURATION = obj({
    "authKind": {"const": "connection"}, "model": TEXT,
    "harness": {"enum": ["pi", "claude", "codex"]},
    "mode": {"enum": ["headless", "interactive"]},
    "connectionRef": TEXT, "configRevision": POSITIVE,
})
NATIVE_CONFIGURATION = obj({
    "authKind": {"const": "native"}, "model": TEXT,
    "harness": {"enum": ["pi", "claude", "codex"]},
    "mode": {"enum": ["headless", "interactive"]}, "nativeAuthRef": TEXT,
})
CONFIGURATION = {"oneOf": [CONNECTION_CONFIGURATION, NATIVE_CONFIGURATION]}
SAVED_CONFIGURATION = {"oneOf": [obj({**shape["properties"],
    "intentDigest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
}) for shape in CONFIGURATION["oneOf"]]}
TARGET = {"name": TEXT, "member": TEXT, "operationId": TEXT}
CONFIGURE_PARAMS = obj({**TARGET, "expectedBindingRevision": REVISION,
    "expectedCellRevision": POSITIVE, "configuration": CONFIGURATION},
    ["name", "operationId", "expectedBindingRevision", "expectedCellRevision", "configuration"])
CANCEL_PARAMS = obj({**TARGET, "expectedBindingRevision": REVISION},
                    ["name", "operationId", "expectedBindingRevision"])
STATUS_PARAMS = obj(TARGET, ["name"])
OPERATION = obj({
    "operationId": TEXT, "phase": {"enum": ["prepared", "finalized", "cancelled"]},
    "requestDigest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "expectedBindingRevision": REVISION, "resultBindingRevision": NULL_REVISION,
    "coordinationPhase": {"enum": [None, "planned", "applied", "cancelling", "compensated"]},
    "expectedCellRevision": NULL_REVISION, "observedCellRevision": NULL_REVISION,
})
RESULT = obj({
    "ok": {"const": True}, "agentId": TEXT, "member": TEXT,
    "binding": {"anyOf": [{"type": "null"}, obj({"bindingRef": TEXT,
        "bindingRevision": POSITIVE, "configuration": SAVED_CONFIGURATION})]},
    "operation": {"anyOf": [{"type": "null"}, OPERATION]},
    "cell": obj({"cellId": TEXT, "revision": NULL_REVISION,
                 "intentMatches": {"type": ["boolean", "null"]}}),
    "cleanupPending": {"type": "boolean"}, "runtime": {"const": "not-verified"},
})
ERRORS = tuple(MethodError(code) for code in (
    codes.INVALID_ARGUMENT, codes.CALLER_NOT_AUTHORIZED, codes.TARGET_NOT_AUTHORIZED,
    codes.AGENT_NOT_FOUND, codes.USER_AGENT_IMMUTABLE, codes.ORG_SOURCE_UNREACHABLE,
    codes.DAEMON_ERROR,
)) + tuple(MethodError(code, obj({"phase": {"const": phase}, "reason": {"enum": reasons}}))
    for code, phase, reasons in (
        (codes.MODEL_CONFIGURATION_INVALID, "validate", ["invalid-connection", "operation-limit-setting"]),
        (codes.MODEL_CONFIGURATION_CONFLICT, "bind", ["revision-mismatch", "cell-revision-mismatch",
            "cell-intent-mismatch", "operation-digest-mismatch", "operation-already-completed",
            "agent-incarnation-changed"]),
        (codes.MODEL_CONFIGURATION_INCOMPLETE, "bind", ["state-unreadable", "destroy-cleanup-pending",
            "operation-in-progress", "cell-outcome-unknown", "coordination-unavailable"]),
        (codes.MODEL_AUTH_UNAVAILABLE, "authorize", ["missing", "revoked", "unreadable", "ambiguous"]),
        (codes.MODEL_COMBINATION_UNSUPPORTED, "admission", ["runtime-option", "mode"]),
        (codes.MODEL_OPERATION_CAPACITY_EXCEEDED, "bind", ["operation-capacity"]),
    ))

CREATE_MODEL_ERRORS = tuple(error for error in ERRORS if error.code in {
    codes.MODEL_CONFIGURATION_INVALID, codes.MODEL_COMBINATION_UNSUPPORTED,
}) + (MethodError(codes.MODEL_CONFIGURATION_INCOMPLETE, obj({
    "phase": {"const": "bind"}, "reason": {"const": "configuration-required"},
    "agentId": TEXT, "actor": TEXT, "operationId": TEXT, "causeCode": TEXT,
})),)
