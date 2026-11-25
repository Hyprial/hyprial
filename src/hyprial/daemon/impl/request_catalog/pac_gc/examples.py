"""PAC GC preview examples using a real closed-workflow selection."""

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

POPULATED = {'ok': True, 'dryRun': True, 'selected': [{'graphId': 'wf-4e335142ba9e43eca026970f7dc5ad5a', 'actor': 'agent:test:machine:wf-4e335142ba9e43eca026970f7dc5ad5a-00e13ed7af55', 'agent': 'wf-4e335142ba9e43eca026970f7dc5ad5a-00e13ed7af55'}], 'selectedCount': 1, 'removableBacklog': 1, 'skipped': {}, 'errors': []}
EMPTY = {'ok': True, 'dryRun': True, 'selected': [], 'selectedCount': 0,
         'removableBacklog': 0, 'skipped': {}, 'errors': []}

EXAMPLES = (MethodExampleSet(method='pac.gc.preview', cases=(
    MethodExample(name='selected', kind='success', description='Preview one closed workflow worker.',
                  params={}, result=POPULATED),
    MethodExample(name='empty', kind='empty', description='There are no eligible workers.',
                  reason='No closed workflow worker is currently eligible.', params={}, result=EMPTY),
    MethodExample(name='non-object', kind='invalid-params', description='IPC params must be an object.',
                  params=[], error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, 'params must be an object')),
    MethodExample(name='unavailable', kind='domain-error', description='The collector is not running.',
                  params={}, error=MethodExampleError('PAC_GC_UNAVAILABLE', 'PAC GC service is unavailable')),
)),)
