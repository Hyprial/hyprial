"""Service-connect request declarations and bindings."""

from functools import partial

from hyprial.daemon.impl.service_connect import ServiceConnectError
from hyprial.daemon.impl.service_grants import handle_service_grants
from hyprial.kernel import DaemonRequestError, IdempotencySpec, MethodError, MethodSpec, ipc_errors

from .schemas import GRANT_PARAMS, RESULTS

_ERRORS = tuple(MethodError(code) for code in (
    ipc_errors.INVALID_ARGUMENT, ipc_errors.SERVICE_INVALID_TARGET, ipc_errors.SERVICE_PORT_IN_USE,
    ipc_errors.SERVICE_NAME_CONFLICT, ipc_errors.SERVICE_UNAVAILABLE, ipc_errors.SERVICE_NOT_REGISTERED,
    ipc_errors.SERVICE_SIDECAR_UNSUPPORTED, ipc_errors.SERVICE_REGISTRY_INVALID,
    ipc_errors.SERVICE_DEVICE_UNAVAILABLE, ipc_errors.SERVICE_NOT_AUTHORIZED,
    ipc_errors.DAEMON_RESTORING, ipc_errors.DAEMON_NOT_READY, ipc_errors.DAEMON_ERROR,
))
_GRANT_ERRORS = {
    "service.grant": tuple(MethodError(code) for code in (
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.PAYLOAD_INVALID,
        ipc_errors.SERVICE_NOT_REGISTERED,
        ipc_errors.SERVICE_DEVICE_UNAVAILABLE,
        ipc_errors.SERVICE_NOT_AUTHORIZED,
        ipc_errors.SERVICE_UNAVAILABLE,
        ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
        ipc_errors.DAEMON_RESTORING,
        ipc_errors.DAEMON_NOT_READY,
        ipc_errors.DAEMON_ERROR,
    )),
    "service.revoke": tuple(MethodError(code) for code in (
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.SERVICE_NOT_REGISTERED,
        ipc_errors.SERVICE_DEVICE_UNAVAILABLE,
        ipc_errors.SERVICE_NOT_AUTHORIZED,
        ipc_errors.SERVICE_REGISTRY_INVALID,
        ipc_errors.SERVICE_UNAVAILABLE,
        ipc_errors.DAEMON_RESTORING,
        ipc_errors.DAEMON_NOT_READY,
        ipc_errors.DAEMON_ERROR,
    )),
    "service.grants": tuple(MethodError(code) for code in (
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.SERVICE_NOT_REGISTERED,
        ipc_errors.SERVICE_DEVICE_UNAVAILABLE,
        ipc_errors.SERVICE_NOT_AUTHORIZED,
        ipc_errors.SERVICE_UNAVAILABLE,
        ipc_errors.DAEMON_RESTORING,
        ipc_errors.DAEMON_NOT_READY,
        ipc_errors.DAEMON_ERROR,
    )),
}


def _spec(name):
    named = name != "service.list"
    return MethodSpec(
        name=name, owner="daemon.service_connect",
        params_schema={"type": "object", "properties": {"name": {"type": "string"}} if named else {},
                       "required": ["name"] if named else [], "additionalProperties": False},
        result_schema=RESULTS[name], errors=_ERRORS, mode="unary", mutates=True,
        retry="never", audience=("local-operator",), http_exposed=False,
        confirmation="none", idempotency=IdempotencySpec(mode="never", scope=name),
        ipc_param_validation="handler",
    )


def _grant_spec(name):
    readonly = name == "service.grants"
    return MethodSpec(
        name=name,
        owner="daemon.service_grants",
        params_schema=GRANT_PARAMS[name],
        result_schema=RESULTS[name],
        errors=_GRANT_ERRORS[name],
        mode="unary",
        mutates=not readonly,
        retry="safe" if readonly else "never",
        audience=("local-operator",),
        http_exposed=False,
        confirmation="none",
        idempotency=IdempotencySpec(mode="safe" if readonly else "never", scope=name),
        ipc_param_validation="schema",
    )


def _handle(app, params, *, method, trusted_message_origin=None):
    try:
        return app._handle_service_connect(method, params)
    except ServiceConnectError as error:
        raise DaemonRequestError(error.code, str(error)) from error


def _handle_grant(app, params, *, method, trusted_message_origin=None):
    return handle_service_grants(app, method, params)


SERVICE_BINDINGS = (
    *(
        (_spec(name), partial(_handle, method=name))
        for name in ("service.connect", "service.disconnect", "service.guide", "service.list")
    ),
    *(
        (_grant_spec(name), partial(_handle_grant, method=name))
        for name in ("service.grant", "service.revoke", "service.grants")
    ),
)
