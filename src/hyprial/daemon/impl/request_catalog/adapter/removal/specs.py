"""Local platform deletion confirms a frozen preview and never removes a remote app."""

from hyprial.kernel import (
    HttpBinding, IdentityBinding, IdempotencySpec, MethodError, MethodSpec,
    PreviewSpec, ipc_errors,
)

STRING = {"type": "string", "minLength": 1}
NULLABLE = {"type": ["string", "null"]}
ERROR_CODES = (
    ipc_errors.INVALID_ARGUMENT, ipc_errors.CALLER_NOT_AUTHORIZED, ipc_errors.ADAPTER_NOT_FOUND,
    ipc_errors.DAEMON_NOT_READY, ipc_errors.DAEMON_ERROR, ipc_errors.PREVIEW_UNKNOWN,
    ipc_errors.PREVIEW_EXPIRED, ipc_errors.PREVIEW_STALE, ipc_errors.OPERATION_IN_PROGRESS,
    ipc_errors.RATE_LIMITED, ipc_errors.DOMAIN_COMMAND_TIMEOUT, ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
)
COMMON = {
    "owner": "daemon.adapters", "audience": ("local-operator",),
    "errors": tuple(MethodError(code) for code in ERROR_CODES), "http_exposed": True,
    "http_binding": HttpBinding(IdentityBinding("none"), readiness="lark-service"),
}
PREVIEW = MethodSpec(
    name="management.adapter.remove.preview", **COMMON,
    params_schema={"type": "object", "properties": {"name": STRING},
                   "required": ["name"], "additionalProperties": False},
    result_schema={"type": "object", "properties": {
        "ok": {"const": True}, "name": STRING, "provider": {"const": "lark"},
        "operationId": STRING, "expiresAt": {"type": "integer", "minimum": 0},
        "summary": STRING, "boundAgent": NULLABLE, "processRunning": {"type": "boolean"},
        "routes": {"type": "array", "items": STRING}, "credentialRemoved": {"type": "boolean"},
    }, "required": ["ok", "name", "provider", "operationId", "expiresAt", "summary",
                    "boundAgent", "processRunning", "routes", "credentialRemoved"], "additionalProperties": False},
    mutates=False, retry="safe", confirmation="preview", preview=PreviewSpec(ttl_seconds=300),
    idempotency=IdempotencySpec(mode="safe", scope="adapter-removal-preview"),
)
EXECUTE = MethodSpec(
    name="management.adapter.remove", **COMMON,
    params_schema={"type": "object", "properties": {"operationId": STRING},
                   "required": ["operationId"], "additionalProperties": False},
    result_schema={"type": "object", "properties": {
        "ok": {"const": True}, "name": STRING, "provider": {"const": "lark"},
        "removed": {"const": True}, "unboundAgent": NULLABLE, "credentialRemoved": {"type": "boolean"},
    }, "required": ["ok", "name", "provider", "removed", "unboundAgent", "credentialRemoved"], "additionalProperties": False},
    mutates=True, retry="never", confirmation="execute", preview_of=PREVIEW.name,
    idempotency=IdempotencySpec(mode="never", scope="adapter-removal-confirmation"),
)
