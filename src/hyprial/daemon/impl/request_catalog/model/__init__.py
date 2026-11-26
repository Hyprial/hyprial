"""One MethodSpec per operation; IPC and HTTP consume the same handler."""

from functools import partial

from hyprial.kernel import HttpBinding, IdentityBinding, IdempotencySpec, MethodSpec
from .schemas import CANCEL_PARAMS, CONFIGURE_PARAMS, ERRORS, RESULT, STATUS_PARAMS

CONFIGURE = MethodSpec(
    name="agent.model.configure", owner="daemon.agent.model", mode="unary",
    mutates=True, retry="never", audience=("local-operator",),
    http_exposed=True, http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none", idempotency=IdempotencySpec(mode="never", scope="model-operation-id"),
    params_schema=CONFIGURE_PARAMS, result_schema=RESULT, errors=ERRORS,
)
STATUS = MethodSpec(
    name="agent.model.status", owner="daemon.agent.model", mode="unary",
    mutates=False, retry="safe", audience=("local-operator",),
    http_exposed=True, http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none", idempotency=IdempotencySpec(mode="safe", scope="model-binding-read"),
    params_schema=STATUS_PARAMS, result_schema=RESULT, errors=ERRORS,
)
CANCEL = MethodSpec(
    name="agent.model.cancel", owner="daemon.agent.model", mode="unary",
    mutates=True, retry="never", audience=("local-operator",),
    http_exposed=True, http_binding=HttpBinding(IdentityBinding("none")),
    confirmation="none", idempotency=IdempotencySpec(mode="never", scope="model-operation-id"),
    params_schema=CANCEL_PARAMS, result_schema=RESULT, errors=ERRORS,
)


def admit(app, params, *, trusted_message_origin=None):
    from hyprial.daemon.impl.provider_keys.ipc import preauthorize

    preauthorize(CONFIGURE.name, app, params, trusted_message_origin=trusted_message_origin)


def handle(app, params, *, action, trusted_message_origin=None):
    from hyprial.daemon.impl.application.model_binding.configure.service import handle as operate

    admit(app, params, trusted_message_origin=trusted_message_origin)
    return operate(app, action, params)


BINDINGS = (
    (CONFIGURE, partial(handle, action="configure")),
    (STATUS, partial(handle, action="status")),
    (CANCEL, partial(handle, action="cancel")),
)
