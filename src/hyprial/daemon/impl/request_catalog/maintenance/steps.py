"""Runtime method declaration and read adapter migrated from settings #2328."""

from __future__ import annotations
from hyprial.kernel import ipc_errors

from hyprial.kernel import IdempotencySpec, MethodError, MethodExample, MethodExampleError, MethodExampleSet, MethodSpec
from collections.abc import Mapping
from typing import Any


_JSON = {"type": ["string", "number", "boolean", "null", "array", "object"]}

_STRING = {"type": "string"}

_NULL_STRING = {"type": ["string", "null"]}

def _record(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required, "additionalProperties": False}

UPGRADE_STEPS_STATUS_SPEC = MethodSpec(
    name="upgrade.steps.status", owner="daemon.upgrade",
    params_schema={"type": "object", "properties": {}, "additionalProperties": _JSON},
    result_schema=_record({
        "steps": {"type": "array", "items": _record({
            "id": _STRING, "state": _STRING, "attempts": {"type": "integer"},
            "updatedAt": _NULL_STRING, "detail": _NULL_STRING,
            "data": {"type": "object", "properties": {}, "additionalProperties": _JSON},
        })},
        "pending": {"type": "array", "items": _STRING},
        "legacyArgvFiles": {"type": "array", "items": _STRING},
        "legacyProcesses": {"type": "array", "items": {
            "type": "object", "properties": {}, "additionalProperties": _JSON,
        }},
    }),
    errors=tuple(MethodError(code) for code in (ipc_errors.INVALID_ARGUMENT, ipc_errors.DAEMON_ERROR, ipc_errors.DAEMON_RESTORING))
        + (MethodError("HTTP_NOT_READY", _record({"reason": _STRING})),),
    mode="unary", mutates=False, retry="safe", audience=("local-operator",), http_exposed=True,
    confirmation="none", idempotency=IdempotencySpec(mode="safe", scope="upgrade-step-status"),
)

UPGRADE_STEP_EXAMPLES = (MethodExampleSet(
    method="upgrade.steps.status",
    empty_not_applicable="The versioned registry always reports its registered steps, even before any migration.",
    cases=(
        MethodExample(name="pending-migration", kind="success", description="Pending records are not an available-version or installation claim.", params={}, result={
            "steps": [{"id": "mcp-entry-v1", "state": "pending", "attempts": 0, "updatedAt": None, "detail": None, "data": {}}],
            "pending": ["mcp-entry-v1"], "legacyArgvFiles": [], "legacyProcesses": [],
        }),
        MethodExample(name="invalid-body", kind="invalid-params", description="Only JSON objects are accepted.",
                      params=[], error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "params must be an object")),
        MethodExample(name="restoring", kind="domain-error", description="Existing restoration gate remains active.",
                      params={}, error=MethodExampleError(ipc_errors.DAEMON_RESTORING, "daemon is restoring")),
    ),
),)


def upgrade_steps_status(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None):
    from hyprial.daemon.impl.upgrade import upgrade_step_status
    from hyprial.daemon.impl.upgrade.steps.mcp_entry import legacy_processes

    status = upgrade_step_status(app.state_dir)
    status["legacyProcesses"] = legacy_processes()
    return status


UPGRADE_STEP_BINDINGS = ((UPGRADE_STEPS_STATUS_SPEC, upgrade_steps_status),)
