"""The existing Agent destroy pair now binds confirmation to an operation ID.

The private IPC socket remains the local administrative boundary. Declaring
these methods does not authenticate a human or expand the worker relay.
Workspace counts describe an observation, not a frozen filesystem snapshot.
"""

from hyprial.identity import AgentError
from hyprial.kernel import (
    HttpBinding, IdentityBinding, IdempotencySpec, MethodError, MethodSpec,
    PreviewSpec, ipc_errors,
)

STRING = {"type": "string", "minLength": 1}
STRINGS = {"type": "array", "items": STRING}
WORKSPACE = {
    "type": "object", "properties": {
        "path": STRING, "exists": {"type": "boolean"},
        "files": {"type": "integer", "minimum": 0},
        "bytes": {"type": "integer", "minimum": 0},
        "unreadableEntries": {"type": "integer", "minimum": 0},
        "deleted": {"type": "boolean"}, "absent": {"type": "boolean"},
    },
    "required": ["path", "exists", "files", "bytes", "unreadableEntries"],
    "additionalProperties": False,
}
ERROR_CODES = (
    ipc_errors.INVALID_ARGUMENT, ipc_errors.CALLER_NOT_AUTHORIZED,
    ipc_errors.AGENT_NOT_FOUND, AgentError.code, ipc_errors.DAEMON_ERROR,
    ipc_errors.PREVIEW_UNKNOWN, ipc_errors.PREVIEW_EXPIRED, ipc_errors.PREVIEW_STALE,
    ipc_errors.OPERATION_IN_PROGRESS, ipc_errors.RATE_LIMITED,
    ipc_errors.AGENT_LAUNCH_IN_PROGRESS, ipc_errors.AGENT_RUNTIME_CONTEXT_STALE,
    ipc_errors.AGENT_DESTROY_SETTLEMENT_UNKNOWN, ipc_errors.DOMAIN_COMMAND_TIMEOUT,
    ipc_errors.SUBMIT_OUTCOME_UNKNOWN, ipc_errors.LIFECYCLE_OPERATION_UNSETTLED,
)
COMMON = {
    "owner": "daemon.agent", "errors": tuple(MethodError(code) for code in ERROR_CODES),
    "audience": ("local-operator", "internal"), "http_exposed": True,
    "http_binding": HttpBinding(IdentityBinding("none")),
}

PREVIEW = MethodSpec(
    name="agent.destroy.preview",
    **{**COMMON, "errors": (*COMMON["errors"], MethodError(
        ipc_errors.USER_AGENT_IMMUTABLE, description="The named agent is the owner's user agent, which is never administered; no data payload."))},
    params_schema={"type": "object", "properties": {"name": STRING},
                   "required": ["name"], "additionalProperties": False},
    result_schema={"type": "object", "properties": {
        "ok": {"const": True}, "actor": STRING, "agent": STRING,
        "operationId": STRING, "expiresAt": {"type": "integer", "minimum": 0},
        "summary": STRING, "workspace": WORKSPACE,
        "adapterBindings": STRINGS, "routines": STRINGS,
        "pendingMessages": {"type": "integer", "minimum": 0},
        "irreversible": {"const": True},
    }, "required": ["ok", "actor", "agent", "operationId", "expiresAt", "summary",
                    "workspace", "adapterBindings", "routines", "pendingMessages", "irreversible"],
    "additionalProperties": False},
    mutates=False, retry="safe", confirmation="preview", preview=PreviewSpec(ttl_seconds=300),
    idempotency=IdempotencySpec(mode="safe", scope="agent-destroy-preview"),
)

EXECUTE = MethodSpec(
    name="agent.destroy", **COMMON,
    params_schema={"type": "object", "properties": {"operationId": STRING},
                   "required": ["operationId"], "additionalProperties": False},
    result_schema={"type": "object", "properties": {
        "ok": {"const": True}, "destroyed": {"const": True},
        "actor": STRING, "agent": STRING, "stopped": STRINGS,
        "destroyedMessages": {"type": "integer", "minimum": 0},
        "unboundAdapters": STRINGS, "removedRoutines": STRINGS,
        "irreversible": {"const": True}, "workspace": WORKSPACE,
    }, "required": ["ok", "destroyed", "actor", "agent", "stopped", "destroyedMessages",
                    "unboundAdapters", "removedRoutines", "irreversible", "workspace"],
    "additionalProperties": False},
    mutates=True, retry="never", confirmation="execute", preview_of=PREVIEW.name,
    idempotency=IdempotencySpec(mode="never", scope="agent-destroy-confirmation"),
)
