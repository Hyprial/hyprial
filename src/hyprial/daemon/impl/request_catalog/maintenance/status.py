"""Runtime method declaration and read adapter migrated from settings #2328."""

from __future__ import annotations

from hyprial.kernel import IdempotencySpec, MethodError, MethodExample, MethodExampleError, MethodExampleSet, MethodSpec, ipc_errors
from collections.abc import Mapping
from typing import Any


DAEMON_STATUS_SPEC = MethodSpec(
    name="daemon.status",
    owner="daemon.status",
    params_schema={"type": "object", "properties": {}, "additionalProperties": False},
    result_schema={
        "type": "object",
        "properties": {
            "running": {"const": True},
            "version": {"type": "string", "minLength": 1},
            "phase": {"type": "string", "enum": ["serving", "reconciled"]},
        },
        "required": ["running", "version", "phase"],
        "additionalProperties": False,
    },
    errors=(
        MethodError(ipc_errors.INVALID_ARGUMENT),
        MethodError(ipc_errors.DAEMON_ERROR),
        MethodError(ipc_errors.DAEMON_RESTORING, description="The IPC restore gate refused dispatch."),
        MethodError(
            "HTTP_NOT_READY",
            {"type": "object", "properties": {"reason": {"type": "string"}},
             "required": ["reason"], "additionalProperties": False},
            description="HTTP catalog admission refused dispatch; RESTORE_IN_PROGRESS means recovery is ongoing.",
        ),
    ),
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator",),
    http_exposed=True,
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="read-only-query"),
)

STATUS_EXAMPLES = (
    MethodExampleSet(
        method="daemon.status",
        empty_not_applicable="A reachable daemon always reports process status; unreachable is a transport error.",
        domain_errors_not_applicable="The projection reads in-memory ping state and has no domain operation.",
        cases=(
            MethodExample(
                name="unexpected-field",
                kind="invalid-params",
                description="This read does not accept command or identity parameters.",
                params={"command": "stop"},
                error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "daemon.status accepts no parameters"),
            ),
            MethodExample(
                name="serving",
                kind="success",
                description="A reachable daemon has finished restoring; no network/model readiness is implied.",
                params={},
                result={"running": True, "version": "0.5.5.3", "phase": "serving"},
            ),
        ),
    ),
)


def daemon_status(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    """Project the real ping response without modifying or duplicating its state."""
    ping = app.handle("ping", {})
    return {"running": ping.get("running"), "version": ping.get("version"), "phase": ping.get("phase")}


STATUS_BINDINGS = ((DAEMON_STATUS_SPEC, daemon_status),)
