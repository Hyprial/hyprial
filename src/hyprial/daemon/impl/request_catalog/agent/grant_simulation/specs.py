"""Wire shapes for the two read-only grant simulations."""

from hyprial.identity import CAPABILITY_ENTRY_POINTS
from hyprial.kernel import IdempotencySpec, MethodError, MethodSpec, ipc_errors


def obj(properties, required):
    return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}


STRING = {'type': 'string'}
NULL_STRING = {'type': ['string', 'null']}
BOOL = {'type': 'boolean'}
COMMON = {'caller': NULL_STRING, 'hosted': BOOL, 'unverified': BOOL}
CHECK_SCENARIO = obj({**COMMON, 'capability': STRING, 'resource': STRING,
                      'scope': {'type': 'array', 'items': STRING}, 'revision': {'type': 'integer'}, 'write': BOOL},
                     [*COMMON, 'capability', 'resource', 'scope', 'revision', 'write'])
VISIBILITY_SCENARIO = obj({**COMMON, 'seeActors': NULL_STRING, 'sendTo': NULL_STRING,
                           'targets': {'type': 'array', 'items': STRING}},
                          [*COMMON, 'seeActors', 'sendTo', 'targets'])
VERDICT = obj({'allowed': BOOL, 'reason': STRING, 'capability': STRING, 'entryPoint': STRING,
               'caller': NULL_STRING, 'resource': STRING, 'revision': {'type': ['integer', 'null']},
               'entryPoints': obj({key: STRING for key in CAPABILITY_ENTRY_POINTS}, list(CAPABILITY_ENTRY_POINTS))},
              ['allowed', 'reason', 'capability', 'entryPoint', 'caller', 'resource', 'revision', 'entryPoints'])
DECISION = obj({'allowed': BOOL, 'reason': STRING, 'capability': STRING, 'caller': NULL_STRING, 'target': STRING},
               ['allowed', 'reason', 'capability', 'caller', 'target'])
ROW = obj({'target': STRING, 'seeActors': DECISION, 'sendTo': DECISION}, ['target', 'seeActors', 'sendTo'])
VISIBILITY = obj({'caller': NULL_STRING, 'hosted': BOOL, 'verified': BOOL,
                  'decisions': {'type': 'array', 'items': ROW}, 'visible': {'type': 'array', 'items': STRING}},
                 ['caller', 'hosted', 'verified', 'decisions', 'visible'])


def spec(name, scenario, result, errors):
    return MethodSpec(name=name, owner='daemon.agent',
        params_schema=obj({'scenario': scenario, 'actor': STRING, 'sessionRef': STRING}, ['scenario']),
        result_schema=result,
        errors=tuple(MethodError(code) for code in (ipc_errors.INVALID_ARGUMENT, *errors,
                                                   ipc_errors.DAEMON_RESTORING, ipc_errors.DAEMON_ERROR)),
        mode='unary', mutates=False, retry='safe', audience=('local-operator', 'bound-agent'),
        http_exposed=False, confirmation='none', idempotency=IdempotencySpec(mode='safe', scope=name))


SPECS = (spec('agent.grant.check', CHECK_SCENARIO, VERDICT,
              (ipc_errors.ENFORCEMENT_ERROR, ipc_errors.ENFORCEMENT_SCOPE_INVALID)),
         spec('agent.grant.visibility', VISIBILITY_SCENARIO, VISIBILITY,
              (ipc_errors.VISIBILITY_ERROR, ipc_errors.VISIBILITY_SCOPE_INVALID)))
