"""Synthetic examples for the two grant simulation requests."""

from hyprial.identity import CAPABILITY_ENTRY_POINTS
from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

COMMON = {'caller': None, 'hosted': False, 'unverified': False}
CHECK = {**COMMON, 'capability': 'tool-surface', 'resource': 'shell', 'scope': [], 'revision': 1, 'write': False}
VISIBILITY = {**COMMON, 'seeActors': None, 'sendTo': None, 'targets': []}


def _decision(capability):
    return {'allowed': True, 'reason': 'host-local operator: no caller identity is imposed',
            'capability': capability, 'caller': None, 'target': 'user:owner'}


EXAMPLES = (
    MethodExampleSet(method='agent.grant.check', cases=(
        MethodExample(name='operator', kind='success', description='Simulate the host-local operator.',
            params={'scenario': CHECK}, result={
                'allowed': True, 'reason': 'host-local operator at harness tool broker: the agent\'s tool allow-list: no caller identity is imposed',
                'capability': 'tool-surface', 'entryPoint': "harness tool broker: the agent's tool allow-list",
                'caller': None, 'resource': 'shell', 'revision': None, 'entryPoints': dict(CAPABILITY_ENTRY_POINTS)}),
        MethodExample(name='non-object', kind='invalid-params', description='IPC params must be an object.',
            params=[], error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, 'params must be an object')),
        MethodExample(name='invalid-scope', kind='domain-error', description='The capability is not recognized.',
            params={'scenario': {**CHECK, 'caller': 'user:owner', 'capability': 'unknown', 'scope': ['bad']}},
            error=MethodExampleError(ipc_errors.ENFORCEMENT_SCOPE_INVALID, "unknown capability 'unknown'")),
        MethodExample(name='invalid-caller', kind='domain-error', description='A simulated caller must be unpadded.',
            params={'scenario': {**CHECK, 'caller': ' '}},
            error=MethodExampleError(ipc_errors.ENFORCEMENT_ERROR, 'principal uri must be a non-empty, unpadded string')),
    ), empty_not_applicable='A capability check always yields a decision.'),
    MethodExampleSet(method='agent.grant.visibility', cases=(
        MethodExample(name='target', kind='success', description='Simulate the operator for one target.',
            params={'scenario': {**VISIBILITY, 'targets': ['user:owner']}},
            result={'caller': None, 'hosted': False, 'verified': True,
                    'decisions': [{'target': 'user:owner', 'seeActors': _decision('see-actors'),
                                   'sendTo': _decision('send-to')}], 'visible': ['user:owner']}),
        MethodExample(name='empty', kind='empty', description='There are no target candidates.',
            reason='A simulation can have no candidate targets.', params={'scenario': VISIBILITY},
            result={'caller': None, 'hosted': False, 'verified': True, 'decisions': [], 'visible': []}),
        MethodExample(name='non-object', kind='invalid-params', description='IPC params must be an object.',
            params=[], error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, 'params must be an object')),
        MethodExample(name='invalid-scope', kind='domain-error', description='The visibility scope must be JSON.',
            params={'scenario': {**VISIBILITY, 'caller': 'user:owner', 'seeActors': 'bad', 'targets': ['user:other']}},
            error=MethodExampleError(ipc_errors.VISIBILITY_SCOPE_INVALID, 'see-actors scope must be one JSON value')),
        MethodExample(name='invalid-caller', kind='domain-error', description='A simulated caller must be unpadded.',
            params={'scenario': {**VISIBILITY, 'caller': ' '}},
            error=MethodExampleError(ipc_errors.VISIBILITY_ERROR, 'caller uri must be a non-empty, unpadded string')),
    )),
)
