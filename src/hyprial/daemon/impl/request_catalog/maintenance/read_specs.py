"""Runtime method declaration and read adapter migrated from settings #2328."""

from __future__ import annotations
from hyprial.kernel import ipc_errors

from hyprial.kernel import IdempotencySpec, MethodError, MethodExample, MethodExampleError, MethodExampleSet, MethodSpec
from .steps import UPGRADE_STEP_EXAMPLES
from collections.abc import Mapping
from typing import Any
from hyprial.kernel import DaemonRequestError
from .steps import UPGRADE_STEP_BINDINGS


_JSON_VALUE = {"type": ["string", "number", "boolean", "null", "array", "object"],
               "description": "Legacy JSON metadata; no new authority is inferred from this value."}

_NULLABLE_STRING = {"type": ["string", "null"]}

_RESULT = {
    "type": "object", "properties": {
        "ok": {"const": True}, "trigger": {"const": "daemon"},
        "environmentPathDigest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "schedule": {"type": "array", "items": {"type": "object", "properties": {
            "hour": {"type": "integer", "minimum": 0, "maximum": 23},
            "minute": {"type": "integer", "minimum": 0, "maximum": 59},
        }, "required": ["hour", "minute"], "additionalProperties": False}},
        "running": {"type": "boolean"}, "active": {"type": "boolean"}, "pending": {"type": "boolean"},
        "nextRunAt": _NULLABLE_STRING, "lastTriggeredAt": _NULLABLE_STRING,
        "lastTrigger": _NULLABLE_STRING, "lastExitCode": {"type": ["integer", "null"]},
        "autoUpgradeEnabled": {"type": ["boolean", "null"]}, "lastSkipReason": _NULLABLE_STRING,
        "lastRun": {"type": ["object", "null"], "properties": {}, "additionalProperties": _JSON_VALUE},
        "lastError": _NULLABLE_STRING,
    },
    "required": ["ok", "trigger", "environmentPathDigest", "schedule", "running", "active", "pending",
                 "nextRunAt", "lastTriggeredAt", "lastTrigger", "lastExitCode", "autoUpgradeEnabled",
                 "lastSkipReason", "lastRun"], "additionalProperties": False,
}

AUTOUPDATE_STATUS_SPEC = MethodSpec(
    name="autoupdate.status", owner="daemon.autoupdate",
    params_schema={"type": "object", "properties": {}, "additionalProperties": _JSON_VALUE},
    result_schema=_RESULT,
    errors=tuple(MethodError(code, description="No data payload.") for code in (
        ipc_errors.INVALID_ARGUMENT, ipc_errors.DAEMON_ERROR, ipc_errors.DAEMON_RESTORING, ipc_errors.DAEMON_NOT_READY,
    )) + (MethodError("HTTP_NOT_READY", {
        "type": "object", "properties": {"reason": {"type": "string"}},
        "required": ["reason"], "additionalProperties": False,
    }, description="HTTP admission refused dispatch."),),
    mode="unary", mutates=False, retry="safe", audience=("local-operator",),
    http_exposed=True, confirmation="none", idempotency=IdempotencySpec(mode="safe", scope="scheduler-status-read"),
)

_STATUS_EXAMPLE = {
    "ok": True, "trigger": "daemon", "environmentPathDigest": "05b927ed78eb58e00f0d7f7e0200723d1bae15a6250e0ad4276332a0a89e1fa1",
    "schedule": [{"hour": 3, "minute": 0}], "running": True, "active": False, "pending": False,
    "nextRunAt": "2026-11-04T03:00:00+00:00", "lastTriggeredAt": None, "lastTrigger": None,
    "lastExitCode": None, "autoUpgradeEnabled": True, "lastSkipReason": None, "lastRun": None,
}

UPGRADE_EXAMPLES = (MethodExampleSet(
    method="autoupdate.status",
    empty_not_applicable="An available scheduler always returns status; a missing scheduler is an error.",
    cases=(
        MethodExample(name="idle", kind="success", description="Scheduler state only, not proof of a new version.",
                      params={}, result=_STATUS_EXAMPLE),
        MethodExample(name="invalid-body", kind="invalid-params", description="A request must be an object.",
                      params=[], error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "params must be an object")),
        MethodExample(name="unavailable", kind="domain-error", description="No update scheduler is installed.",
                      params={}, error=MethodExampleError(ipc_errors.DAEMON_NOT_READY, "The update scheduler is not available.")),
    ),
),)

UPGRADE_EXAMPLES = (*UPGRADE_EXAMPLES, *UPGRADE_STEP_EXAMPLES)


def autoupdate_status(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None) -> dict[str, Any]:
    if getattr(app, "_autoupdate", None) is None:
        raise DaemonRequestError(ipc_errors.DAEMON_NOT_READY, "The update scheduler is not available.")
    return app._ipc_autoupdate_status()


UPGRADE_BINDINGS = ((AUTOUPDATE_STATUS_SPEC, autoupdate_status), *UPGRADE_STEP_BINDINGS)
