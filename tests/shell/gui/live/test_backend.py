"""The live host retains UI intent, never a second transcript or send queue."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from hyprial.shell.impl.gui.live.backend import LiveBackend


TARGET = 'agent:alice@example.test:node:squire'
OWNER = 'user:alice@example.test'


class Adapter:
    def __init__(self):
        self.calls = 0
        self.accepted = False

    def create_session(self, target):
        assert target == TARGET
        return {'sessionId': 'session', 'targetUri': target}

    def session_binding(self, session):
        from hyprial.shell.impl.gui.adapter.chat import SessionBinding
        assert session == 'session'
        return SessionBinding((TARGET,), 'conversation')

    def send(self, session, request):
        from hyprial.shell.impl.gui.adapter.errors import AppAdapterUnavailable
        self.calls += 1
        self.accepted = True
        raise AppAdapterUnavailable('response-lost')

    def send_lookup(self, session, request):
        if not self.accepted:
            return {'status': 'unknown', 'reason': 'not-found'}
        return {'status': 'accepted', 'commandId': request['idempotencyKey'],
                'conversationId': 'conversation', 'messageId': 'message'}


def make(tmp_path, adapter):
    return LiveBackend(adapter=adapter, request=lambda *_: {},
                       principal=OWNER, state_dir=tmp_path)


def frame(operation, **params):
    return {'operation': operation, 'params': params}


def send_frame(text='hello', key='original'):
    return frame('sessions.send', sessionId='session',
                 request={'text': text, 'idempotencyKey': key})


def test_accept_then_lost_response_recovers_after_host_restart(tmp_path):
    adapter = Adapter()
    host = make(tmp_path, adapter)
    host.handle(frame('sessions.create', targetUri=TARGET))
    assert host.handle(send_frame())['error']['definitive'] is False
    recovered = make(tmp_path, adapter)
    pending = recovered.handle(frame('sessions.pending'))['result']['requests']
    assert pending[0]['request'] == {'text': 'hello', 'idempotencyKey': 'original'}
    recovered.handle(frame('sessions.create', targetUri=TARGET))
    result = recovered.handle({**send_frame(), 'operation': 'sessions.send.lookup'})
    assert result['result']['messageId'] == 'message'
    assert len(recovered.handle(frame('sessions.pending'))['result']['requests']) == 1
    recovered.handle({**send_frame(), 'operation': 'sessions.send.confirm'})
    assert recovered.handle(frame('sessions.pending'))['result']['requests'] == []
    assert adapter.calls == 1


def test_concurrent_duplicate_never_resends_and_changed_text_refused(tmp_path):
    adapter = Adapter()
    host = make(tmp_path, adapter)
    host.handle(frame('sessions.create', targetUri=TARGET))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: host.handle(send_frame()), range(2)))
    assert adapter.calls == 1
    result = host.handle(send_frame(text='different'))
    assert result['ok'] is False
    assert adapter.calls == 1


def test_no_record_does_not_enable_retry(tmp_path):
    adapter = Adapter()
    host = make(tmp_path, adapter)
    host.handle(frame('sessions.create', targetUri=TARGET))
    result = host.handle({**send_frame(), 'operation': 'sessions.send.lookup'})
    assert result['result']['status'] == 'unknown'
    assert adapter.calls == 0


@pytest.mark.parametrize('bad', [
    {'operation': 'message.send', 'params': {}},
    {'operation': 'sessions.send', 'params': {'from': OWNER}},
    {'operation': 'targets.list', 'params': {'principal': OWNER}},
    {'operation': 'sessions.pending', 'params': {}, 'sender': OWNER},
])
def test_unknown_operations_and_identity_injection_refused(tmp_path, bad):
    assert make(tmp_path, Adapter()).handle(bad)['ok'] is False


def test_targets_list_marks_only_trusted_personal_squire(tmp_path):
    class DirectoryAdapter(Adapter):
        def targets_list(self):
            return {'targets': [
                {'targetUri': 'agent:bob@example.test:remote:squire', 'status': 'online'},
                {'targetUri': TARGET, 'status': 'online'},
            ]}

    host = LiveBackend(adapter=DirectoryAdapter(), request=lambda *_: {},
                       principal=OWNER, state_dir=tmp_path,
                       personal_squire_uri=TARGET)
    assert host.handle(frame('targets.list'))['result']['preferredTargetUri'] == TARGET

    absent = LiveBackend(adapter=DirectoryAdapter(), request=lambda *_: {},
                         principal=OWNER, state_dir=tmp_path,
                         personal_squire_uri='agent:alice@example.test:other:squire')
    assert absent.handle(frame('targets.list'))['result']['preferredTargetUri'] is None


def test_journal_is_scoped_to_trusted_principal(tmp_path):
    host = make(tmp_path, Adapter())
    host.handle(frame('sessions.create', targetUri=TARGET))
    host.handle(send_frame())
    other = LiveBackend(adapter=Adapter(), request=lambda *_: {},
                        principal='user:bob@example.test', state_dir=tmp_path)
    assert other.handle(frame('sessions.pending'))['result']['requests'] == []


def test_other_browser_cannot_submit_new_key_while_original_is_unknown(tmp_path):
    adapter = Adapter()
    host = make(tmp_path, adapter)
    host.handle(frame('sessions.create', targetUri=TARGET))
    host.handle(send_frame())
    second = make(tmp_path, adapter)
    second.handle(frame('sessions.create', targetUri=TARGET))
    assert second.handle(send_frame(key='different-key'))['ok'] is False
    assert adapter.calls == 1


def test_exception_after_adapter_invocation_never_means_not_sent(tmp_path):
    adapter = Adapter()
    def malformed(*_):
        raise ValueError('bad response after write')
    adapter.send = malformed
    host = make(tmp_path, adapter)
    host.handle(frame('sessions.create', targetUri=TARGET))
    assert host.handle(send_frame())['error']['definitive'] is False
    assert len(host.handle(frame('sessions.pending'))['result']['requests']) == 1


def test_valid_host_ack_lost_over_http_remains_recoverable_until_browser_confirms(tmp_path):
    adapter = Adapter()
    adapter.accepted = True
    adapter.send = adapter.send_lookup
    host = make(tmp_path, adapter)
    host.handle(frame('sessions.create', targetUri=TARGET))
    assert host.handle(send_frame())['result']['status'] == 'accepted'
    # The HTTP response is discarded. A new browser/host still has frozen intent.
    recovered = make(tmp_path, adapter)
    assert recovered.handle(frame('sessions.pending'))['result']['requests'][0]['request'] == {
        'text': 'hello', 'idempotencyKey': 'original'}
    recovered.handle(frame('sessions.create', targetUri=TARGET))
    assert recovered.handle({**send_frame(), 'operation': 'sessions.send.confirm'})['result']['messageId'] == 'message'
    assert recovered.handle(frame('sessions.pending'))['result']['requests'] == []


@pytest.mark.parametrize('message_id', [None, ''])
def test_incomplete_acceptance_cannot_scrub_original_request(tmp_path, message_id):
    adapter = Adapter()
    adapter.send = lambda *_: {'status': 'accepted', 'commandId': 'original',
                              'conversationId': 'conversation', 'messageId': message_id}
    host = make(tmp_path, adapter)
    host.handle(frame('sessions.create', targetUri=TARGET))
    assert host.handle(send_frame())['error']['definitive'] is False
    assert make(tmp_path, adapter).handle(frame('sessions.pending'))['result']['requests'][0]['request']['text'] == 'hello'
