"""The existing bounded PAC GC preview, exposed through the request catalog.

Observation may advance a publicly visible in-memory projection revision, so
the method is declared mutating and non-retryable. It does not run GC removal
or persist a GC mutation.
"""

from hyprial.kernel import IdempotencySpec, MethodError, MethodSpec, ipc_errors


def obj(properties, required):
    return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}


STRING = {'type': 'string'}
COUNT = {'type': 'integer', 'minimum': 0}
ROW = obj({'graphId': STRING, 'actor': STRING, 'agent': STRING}, ['graphId', 'actor', 'agent'])
ERROR = obj({'graphId': STRING, 'errorType': STRING, 'detail': STRING}, ['graphId', 'errorType', 'detail'])
RESULT = obj({
    'ok': {'const': True}, 'dryRun': {'const': True}, 'selected': {'type': 'array', 'items': ROW},
    'selectedCount': COUNT, 'removableBacklog': COUNT,
    'skipped': {'type': 'object', 'additionalProperties': COUNT}, 'errors': {'type': 'array', 'items': ERROR},
}, ['ok', 'dryRun', 'selected', 'selectedCount', 'removableBacklog', 'skipped', 'errors'])

SPEC = MethodSpec(
    name='pac.gc.preview', owner='daemon.pac',
    params_schema={'type': 'object', 'properties': {}, 'required': [],
                   'additionalProperties': {'type': ['string', 'number', 'boolean', 'null', 'array', 'object']}},
    result_schema=RESULT,
    errors=tuple(MethodError(code) for code in
                 (ipc_errors.INVALID_ARGUMENT, 'PAC_GC_UNAVAILABLE', ipc_errors.DAEMON_RESTORING, ipc_errors.DAEMON_ERROR)),
    mode='unary', mutates=True, retry='never', audience=('local-operator',), http_exposed=False,
    confirmation='none', idempotency=IdempotencySpec(mode='never', scope='pac.gc.preview'),
)


def preview(app, params, *, trusted_message_origin=None):
    return app._ipc_pac_gc_preview()


BINDINGS = ((SPEC, preview),)
