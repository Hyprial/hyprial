"""Declared upgrade methods: one runner and an explicit target Hyprial home."""
from pathlib import Path

from hyprial.daemon.impl.upgrade import run_upgrade_steps, upgrade_step_status
from hyprial.daemon.impl.upgrade.steps.mcp_entry import legacy_processes
from hyprial.kernel import IdempotencySpec, MethodError, MethodSpec, ipc_errors

_RESULT = {
    "type": "object", "required": ["steps", "pending"],
    "properties": {
        "steps": {"type": "array", "items": {"type": "object"}},
        "pending": {"type": "array", "items": {"type": "string"}},
        "legacyArgvFiles": {"type": "array", "items": {"type": "string"}},
        "legacyProcesses": {"type": "array", "items": {"type": "object"}},
    }, "additionalProperties": False,
}
_REFUSED = {
    "type": "object", "required": ["ok", "code", "error"],
    "properties": {"ok": {"const": False}, "code": {"const": ipc_errors.INVALID_ARGUMENT},
                   "error": {"type": "string"}}, "additionalProperties": False,
}


def _spec(name, mutates):
    return MethodSpec(name=name, owner="daemon.upgrade",
        params_schema={"type": "object", "properties": {"apply": {"type": "boolean"}} if mutates else {},
                       "additionalProperties": False},
        result_schema={"type": "object", "properties": {**_RESULT["properties"], **_REFUSED["properties"]},
                       "additionalProperties": False, "oneOf": [_RESULT, _REFUSED]} if mutates else _RESULT,
        errors=(MethodError(ipc_errors.INVALID_ARGUMENT), MethodError(ipc_errors.DAEMON_NOT_READY)),
        mode="unary", mutates=mutates, retry="never" if mutates else "safe",
        audience=("local-operator",), http_exposed=False, confirmation="none",
        idempotency=IdempotencySpec(mode="never" if mutates else "safe", scope=name))


def status(app, params, *, trusted_message_origin=None):
    result = upgrade_step_status(app.state_dir)
    result["legacyProcesses"] = legacy_processes()
    return result


def run(app, params, *, trusted_message_origin=None):
    if not params.get("apply", True):
        return {"ok": False, "code": ipc_errors.INVALID_ARGUMENT, "error": "apply required"}
    from hyprial.daemon.impl.org.membership.migration.runtime import run as migrate
    return run_upgrade_steps(app.state_dir, home=Path.home(),
                             hyprial_home=Path(app.hyprial_home),
                             membership_migration=lambda: migrate(app))


UPGRADE_BINDINGS = ((_spec("upgrade.steps.run", True), run),)
