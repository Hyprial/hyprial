"""Routine request bindings that preserve legacy admission."""

from functools import partial

from hyprial.daemon.impl.ipc.params import _required_string
from hyprial.identity import PAC_PRINCIPAL_UNVERIFIED
from hyprial.kernel import DaemonRequestError, IdempotencySpec, MethodError, MethodSpec, canonical_user_uri, ipc_errors

from .schemas import RESULTS, params_schema

_PRINCIPAL_DATA = {"type": "object", "properties": {"cause": {"type": "string"}},
                   "required": ["cause"], "additionalProperties": False}
def _errors(name):
    codes = [ipc_errors.INVALID_ARGUMENT, ipc_errors.ROUTINE_UNAVAILABLE,
             ipc_errors.DAEMON_RESTORING, ipc_errors.DAEMON_ERROR]
    if name != "routine.audit":
        codes.extend((ipc_errors.CALLER_NOT_AUTHORIZED, "ROUTINE_NOT_FOUND",
                      "ROUTINE_OVERLOADED", "ROUTINE_CLOSING", "ROUTINE_COMMAND_TIMEOUT",
                      "ROUTINE_PROTOCOL_ERROR"))
    if name == "routine.resume":
        codes.extend(("ROUTINE_QUARANTINED", ipc_errors.USER_AGENT_IMMUTABLE))
    return (MethodError(PAC_PRINCIPAL_UNVERIFIED, _PRINCIPAL_DATA),
            *(MethodError(code) for code in codes))


def _spec(name):
    mutates = name != "routine.audit"
    return MethodSpec(
        name=name, owner="daemon.routine", params_schema=params_schema(name),
        result_schema=RESULTS[name], errors=_errors(name), mode="unary", mutates=mutates,
        retry="never" if mutates else "safe", audience=("local-operator", "bound-agent"),
        http_exposed=False, confirmation="none", ipc_param_validation="handler",
        idempotency=IdempotencySpec(mode="never" if mutates else "safe", scope=name),
    )


def _handle(app, params, *, method, trusted_message_origin=None):
    caller = app._workflow_caller(params)
    if method != "routine.audit" and app._routine_service is not None:
        current = app._routine_service.status(name=_required_string(params.get("name"), "name"))
        if caller != canonical_user_uri(app.owner) and not app._routine_caller_is(
            caller, current["owner"], current.get("actor")
        ):
            raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "caller does not own this routine")
    if method == "routine.audit":
        return app._ipc_routine_audit()
    if method == "routine.pause":
        return app._ipc_routine_pause(params)
    return app._ipc_routine_resume(params)


ROUTINE_BINDINGS = tuple(
    (_spec(name), partial(_handle, method=name))
    for name in ("routine.audit", "routine.pause", "routine.resume")
)
