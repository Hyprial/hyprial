"""Pure settings contract declarations; importing this module binds no runtime handler."""
from hyprial.kernel import ipc_errors

from hyprial.kernel import HttpBinding, IdentityBinding, IdempotencySpec, MethodError, MethodSpec

from .schemas import NULL_STRING, STRING, USER, record

_CONFIRMER = {'type': 'string', 'minLength': 1, 'x-hyprial-http-forbidden': True}

_ERRORS = tuple(MethodError(code) for code in (
    ipc_errors.INVALID_ARGUMENT, ipc_errors.IDENTITY_SOURCE_UNAVAILABLE, ipc_errors.IDENTITY_OVERRIDE_FORBIDDEN,
    'USER_EXISTS', 'USER_OWNER_REQUIRED', 'USER_OWNER_FORBIDDEN',
    ipc_errors.DAEMON_NOT_READY, ipc_errors.DAEMON_ERROR, ipc_errors.DAEMON_RESTORING,
)) + (MethodError('HTTP_NOT_READY', record({'reason': STRING})),)

USERS_ADD_SPEC = MethodSpec(name='identity.users.add', owner='daemon.identity', params_schema=record({
    'kind': {'type': 'string', 'enum': ['member', 'guest']}, 'confirmedBy': _CONFIRMER,
    'owner': NULL_STRING, 'displayName': NULL_STRING, 'nickname': NULL_STRING, 'realName': NULL_STRING,
}, ['kind', 'confirmedBy']), result_schema=record({'user': USER}),
    errors=_ERRORS, mode='unary', mutates=True, retry='never', audience=('local-operator',),
    http_exposed=True, http_binding=HttpBinding(IdentityBinding('local-owner-selector', 'confirmedBy')),
    confirmation='none', ipc_param_validation='handler', idempotency=IdempotencySpec(mode='never'))
