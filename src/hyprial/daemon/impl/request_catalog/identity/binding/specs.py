"""Identity IPC schemas preserve the legacy handler's parameter semantics."""

from hyprial.kernel import IdempotencySpec, MethodError, MethodSpec, ipc_errors

from ..schemas import record

_STRING = {"type": "string"}
_ANY_JSON = {"type": ["string", "number", "boolean", "null", "array", "object"]}


WHOAMI = MethodSpec(
    name="identity.whoami", owner="daemon.identity",
    params_schema={"type": "object", "properties": {
        "actor": _ANY_JSON, "from": _ANY_JSON, "sessionRef": _ANY_JSON,
    }, "additionalProperties": _ANY_JSON},
    result_schema=record({
        "ok": {"const": True}, "actor": _STRING, "sessionRef": _STRING,
        "nodeId": _STRING, "sessionRegistered": {"type": "boolean"},
        "daemonEpoch": _STRING, "channelCurrentThisGeneration": {"type": "boolean"},
        "channelCurrentEpoch": {"type": ["string", "null"]},
    }, ["ok", "actor", "sessionRef", "nodeId", "sessionRegistered", "daemonEpoch"]),
    errors=tuple(MethodError(code) for code in (
        ipc_errors.INVALID_ARGUMENT, ipc_errors.DAEMON_ERROR, ipc_errors.DAEMON_RESTORING,
    )), mode="unary", mutates=False, retry="safe",
    audience=("local-operator", "bound-agent"), http_exposed=False,
    confirmation="none", ipc_param_validation="handler",
    idempotency=IdempotencySpec(mode="safe", scope="session-identity-read"),
)
