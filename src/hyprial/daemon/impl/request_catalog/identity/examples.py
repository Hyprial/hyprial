"""Pure settings contract declarations; importing this module binds no runtime handler."""

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

_IDENTITY_USER = {'userKey': 'example-member', 'kind': 'member', 'owner': 'example@test.invalid',
    'nickname': None, 'realName': None, 'displayName': 'Example member', 'createdAtMs': 1, 'updatedAtMs': 1}

IDENTITY_EXAMPLES = (MethodExampleSet(method='identity.users.list', cases=(
    MethodExample(name='listed', kind='success', description='Existing authoritative records.', params={},
        result={'users': [_IDENTITY_USER]}),
    MethodExample(name='empty', kind='empty', description='The current authority contains no matching records.',
        reason='No matching records.', params={}, result={'users': []}),
    MethodExample(name='non-object-params', kind='invalid-params',
        description='The registry rejects non-object params before legacy IPC admission.',
        params=[], error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, 'identity.users.list params must be an object')),
    MethodExample(name='source-unavailable', kind='domain-error', description='Unreadable identity source is not an empty list.',
        params={}, error=MethodExampleError(ipc_errors.IDENTITY_SOURCE_UNAVAILABLE, 'identity source is unavailable')),
)),)
