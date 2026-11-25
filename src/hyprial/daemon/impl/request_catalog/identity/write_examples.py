"""Pure settings contract declarations; importing this module binds no runtime handler."""
from hyprial.kernel import ipc_errors

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet

from .examples import _IDENTITY_USER

_PARAMS = {'kind': 'member', 'owner': 'example@test.invalid', 'confirmedBy': 'operator'}

IDENTITY_WRITE_EXAMPLES = (MethodExampleSet(method='identity.users.add',
    empty_not_applicable='Mutation returns a receipt, not a list.', cases=(
    MethodExample(name='committed', kind='success', description='Local authority receipt; not organization publication.',
        params=_PARAMS, result={'user': _IDENTITY_USER}),
    MethodExample(name='non-object-params', kind='invalid-params',
        description='The registry rejects non-object params before legacy IPC admission.',
        params=[], error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, 'identity.users.add params must be an object')),
    MethodExample(name='refused', kind='domain-error', description='Existing authority refused the mutation.',
        params=_PARAMS, error=MethodExampleError('USER_EXISTS', 'Identity mutation refused')),
)),)
