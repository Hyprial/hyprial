"""Read-only access to the existing legacy workflow archive."""

from hyprial.identity import PAC_PRINCIPAL_UNVERIFIED
from hyprial.kernel import IdempotencySpec, MethodError, MethodExample, MethodExampleError, MethodExampleSet, MethodSpec, ipc_errors

_JSON = {"type": ["string", "number", "boolean", "null", "array", "object"]}
_SQL_SCALAR = {"type": ["string", "number", "null"]}
_ROW = {
    "type": "object", "properties": {
        "run_id": _SQL_SCALAR, "name": _SQL_SCALAR, "state": _SQL_SCALAR,
        "sender": _SQL_SCALAR, "created_at_ms": _SQL_SCALAR,
        "finished_at_ms": _SQL_SCALAR, "runId": _SQL_SCALAR,
        "backend": {"const": "legacy-archive"}, "readOnly": {"const": True},
    },
    "required": ["run_id", "name", "state", "sender", "created_at_ms", "finished_at_ms", "runId", "backend", "readOnly"],
    "additionalProperties": False,
}
_PRINCIPAL_DATA = {"type": "object", "properties": {"cause": {"type": "string"}},
                   "required": ["cause"], "additionalProperties": False}
HISTORY_LIST = MethodSpec(
    name="workflow.history.list", owner="daemon.workflow",
    params_schema={"type": "object", "properties": {"actor": _JSON, "sessionRef": _JSON, "limit": _JSON}, "additionalProperties": _JSON},
    result_schema={"type": "object", "properties": {"runs": {"type": "array", "items": _ROW}},
                   "required": ["runs"], "additionalProperties": False},
    errors=(MethodError(PAC_PRINCIPAL_UNVERIFIED, _PRINCIPAL_DATA),
            MethodError(ipc_errors.INVALID_ARGUMENT), MethodError(ipc_errors.DAEMON_ERROR),
            MethodError(ipc_errors.DAEMON_RESTORING)),
    mode="unary", mutates=False, retry="safe", audience=("local-operator", "bound-agent"),
    http_exposed=False, confirmation="none", ipc_param_validation="handler",
    idempotency=IdempotencySpec(mode="safe", scope="workflow-history"),
)


def history_list(app, params, *, trusted_message_origin=None):
    return app._ipc_workflow_status_group("workflow.history.list", dict(params))


BINDINGS = ((HISTORY_LIST, history_list),)
EXAMPLES = (MethodExampleSet(
    method="workflow.history.list",
    cases=(
        MethodExample(name="archive", kind="success", description="A legacy archive row visible to the owner.",
            params={"actor": "user:op"}, result={"runs": [{"run_id": "own", "name": "one", "state": "done", "sender": "user:op", "created_at_ms": 2, "finished_at_ms": None, "runId": "own", "backend": "legacy-archive", "readOnly": True}]}),
        MethodExample(name="empty", kind="empty", description="No legacy runs exist.",
            params={"actor": "user:op"}, result={"runs": []}, reason="The archive can be absent or empty."),
        MethodExample(name="non-object", kind="invalid-params", description="IPC params must be an object.",
            params=[], error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "params must be an object")),
        MethodExample(name="missing-caller", kind="domain-error", description="A claimed caller must have a live session.",
            params={}, error=MethodExampleError(PAC_PRINCIPAL_UNVERIFIED, "workflow actor claim has no matching live daemon session", data={"cause": ipc_errors.CALLER_NOT_AUTHORIZED})),
    ),
),)
