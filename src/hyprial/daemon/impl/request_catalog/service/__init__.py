"""Service-connect request declarations and bindings."""

from functools import partial

from hyprial.daemon.impl.service_connect import ServiceConnectError
from hyprial.kernel import DaemonRequestError, IdempotencySpec, MethodError, MethodSpec, ipc_errors

from .schemas import RESULTS

_ERRORS = tuple(MethodError(code) for code in (
    ipc_errors.INVALID_ARGUMENT, ipc_errors.SERVICE_INVALID_TARGET, ipc_errors.SERVICE_PORT_IN_USE,
    ipc_errors.SERVICE_NAME_CONFLICT, ipc_errors.SERVICE_UNAVAILABLE, ipc_errors.SERVICE_NOT_REGISTERED,
    ipc_errors.SERVICE_SIDECAR_UNSUPPORTED, ipc_errors.SERVICE_REGISTRY_INVALID,
    ipc_errors.SERVICE_DEVICE_UNAVAILABLE, ipc_errors.SERVICE_NOT_AUTHORIZED,
    ipc_errors.DAEMON_RESTORING, ipc_errors.DAEMON_NOT_READY, ipc_errors.DAEMON_ERROR,
))


def _spec(name):
    if name == "service.grant":
        properties = {
            "name": {"type": "string"},
            "recipientKey": {"type": "string"},
            "secretB64": {"type": "string"},
        }
        required = ["name", "recipientKey", "secretB64"]
    else:
        named = name != "service.list"
        properties = {"name": {"type": "string"}} if named else {}
        required = ["name"] if named else []
    return MethodSpec(
        name=name, owner="daemon.service_connect",
        params_schema={"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False},
        result_schema=RESULTS[name], errors=_ERRORS, mode="unary", mutates=True,
        retry="never", audience=("local-operator",), http_exposed=False,
        confirmation="none", idempotency=IdempotencySpec(mode="never", scope=name),
        ipc_param_validation="handler",
    )


def _handle(app, params, *, method, trusted_message_origin=None):
    try:
        return app._handle_service_connect(method, params)
    except ServiceConnectError as error:
        raise DaemonRequestError(error.code, str(error)) from error


SERVICE_BINDINGS = tuple(
    (_spec(name), partial(_handle, method=name))
    for name in (
        "service.connect",
        "service.disconnect",
        "service.grant",
        "service.guide",
        "service.list",
    )
)
