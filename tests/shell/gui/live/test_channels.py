"""No channel success without grants, a verified binding, and runtime state."""
from types import SimpleNamespace

from hyprial.kernel import DaemonRequestError
from hyprial.shell.impl.gui.live.backend import LiveBackend


ACTOR = 'agent:test:node:worker'


class Authorization:
    def status(self, name):
        return {'adapter': name, 'appId': 'app', 'authorization': 'authorized',
                'capabilities': ['send'], 'checkedAt': 123}

    def current_operation(self, name):
        return None


def host(tmp_path, *, conditional=True, conflict=False, lost=False):
    calls = []
    actor = None

    def request(method, params):
        nonlocal actor
        calls.append((method, params))
        if method == 'adapter.list':
            return {'ok': True, 'adapters': [{'name': 'test', 'provider': 'lark'}]}
        if method == 'adapter.pins':
            return {'ok': True, 'pins': {'test': actor}, 'supportsExpectedActor': conditional}
        if method == 'adapter.status':
            return {'ok': True, 'adapter': {'name': 'test', 'provider': 'lark', 'online': False}}
        if method == 'adapter.pin':
            if conflict:
                raise DaemonRequestError('PIN_STATE_CONFLICT', 'old actor changed')
            actor = params['actor']
            if lost:
                raise OSError('accepted but response lost')
            return {'ok': True}
        raise AssertionError(method)

    backend = LiveBackend(adapter=SimpleNamespace(targets_list=lambda: {
        'targets': [{'targetUri': ACTOR}]}), request=request, principal='user:test',
        state_dir=tmp_path, authorization=Authorization())
    return backend, calls


def bind(backend, **extra):
    return backend.handle({'operation': 'lark.binding.set',
        'params': {'adapter': 'test', 'agentId': ACTOR, 'expectedActor': None, **extra}})


def test_old_daemon_cannot_silently_ignore_binding_condition(tmp_path):
    backend, calls = host(tmp_path, conditional=False)
    assert bind(backend)['error']['definitive'] is False
    assert all(method != 'adapter.pin' for method, _ in calls)


def test_binding_condition_is_forwarded_and_receipt_does_not_claim_online(tmp_path):
    backend, calls = host(tmp_path)
    result = bind(backend)['result']
    assert result['binding']['actor'] == ACTOR
    assert result['runtime']['online'] is False
    assert ('adapter.pin', {'name': 'test', 'actor': ACTOR, 'expectedActor': None}) in calls


def test_conflict_requires_fresh_confirmation_without_retry(tmp_path):
    backend, calls = host(tmp_path, conflict=True)
    assert bind(backend)['error'] == {'code': 'binding-conflict',
        'message': '请求被拒绝，请检查当前状态。', 'definitive': True, 'canRetry': False}
    assert sum(method == 'adapter.pin' for method, _ in calls) == 1


def test_lost_pin_response_reconciles_by_read_without_repeating_write(tmp_path):
    backend, calls = host(tmp_path, lost=True)
    assert bind(backend)['error']['definitive'] is False
    result = backend.handle({'operation': 'lark.adapters', 'params': {}})
    assert result['result']['adapters'][0]['binding']['actor'] == ACTOR
    assert sum(method == 'adapter.pin' for method, _ in calls) == 1
