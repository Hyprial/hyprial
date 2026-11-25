"""Pure settings contract declarations; importing this module binds no runtime handler."""

from hyprial.kernel import HttpBinding, IdentityBinding, IdempotencySpec, MethodError, MethodSpec, ipc_errors

from .schemas import NULL_STRING, USERS_RESULT, record

_ERRORS = tuple(MethodError(code) for code in (
    ipc_errors.INVALID_ARGUMENT, ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
    ipc_errors.DAEMON_NOT_READY, ipc_errors.DAEMON_ERROR, ipc_errors.DAEMON_RESTORING,
)) + (MethodError('HTTP_NOT_READY', record({'reason': {'type': 'string'}})),)

USERS_LIST_SPEC = MethodSpec(name='identity.users.list', owner='daemon.identity',
    params_schema=record({'kind': NULL_STRING, 'user': NULL_STRING}, []), result_schema=USERS_RESULT,
    errors=_ERRORS, mode='unary', mutates=False, retry='safe', audience=('local-operator',),
    http_exposed=True, http_binding=HttpBinding(IdentityBinding('none')), confirmation='none',
    idempotency=IdempotencySpec(mode='safe', scope='read-only-query'))
