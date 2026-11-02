"""Allowlisted operations consumed by the local HTTP bridge."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from hyprial.kernel import DaemonRequestError
from hyprial.shell.impl.gui.adapter.errors import AppAdapterError, AppAdapterUnavailable
from hyprial.shell.impl.gui.adapter.host import handle_request
from hyprial.shell.impl.gui.live.journal import IntentJournal


FIELDS = {
    'targets.list': set(), 'sessions.create': {'targetUri'},
    'sessions.history': {'sessionId', 'args'}, 'sessions.pending': set(),
    'sessions.send': {'sessionId', 'request'},
    'sessions.send.lookup': {'sessionId', 'request'},
    'sessions.send.confirm': {'sessionId', 'request'},
    'lark.adapters': set(), 'lark.authorization.status': {'adapter'},
    'lark.authorization.start': {'adapter'},
    'lark.authorization.operation': {'operationId'},
    'lark.authorization.cancel': {'operationId'},
    'lark.binding.set': {'adapter', 'agentId', 'expectedActor'},
}


def failure(code, message, *, definitive=False):
    return {'ok': False, 'error': {'code': code, 'message': message,
                                 'definitive': definitive, 'canRetry': False}}


class LiveBackend:
    def __init__(self, *, adapter, request, principal, state_dir: Path,
                 authorization=None, personal_squire_uri=None):
        self.adapter = adapter
        self.request = request
        self.authorization = authorization
        self.personal_squire_uri = personal_squire_uri
        self.journal = IntentJournal(state_dir, principal)

    def handle(self, frame):
        try:
            if not isinstance(frame, Mapping) or set(frame) != {'operation', 'params'}:
                raise ValueError('expected operation and params only')
            operation, params = frame['operation'], frame['params']
            if not isinstance(operation, str) or operation not in FIELDS:
                raise ValueError('unsupported GUI operation')
            if not isinstance(params, dict) or set(params) - FIELDS[operation]:
                raise ValueError('unexpected GUI request fields')
            if operation.startswith('lark.'):
                from hyprial.shell.impl.gui.live.channels import channel_operation
                result = channel_operation(self, operation, params)
            elif operation == 'targets.list':
                directory = self.adapter.targets_list()
                preferred = self.personal_squire_uri
                result = {**directory, 'preferredTargetUri': preferred if any(
                    row['targetUri'] == preferred for row in directory['targets']) else None}
            elif operation == 'sessions.pending':
                result = {'requests': [
                    {'sessionId': row['session'], 'targetUri': row['target'],
                     'request': {'text': row['text'], 'idempotencyKey': row['key']},
                     'status': 'unknown'} for row in self.journal.pending()
                ]}
            elif operation == 'sessions.create':
                recovered = next((row for row in self.journal.pending()
                                  if row['target'] == params.get('targetUri')), None)
                if recovered and self.adapter.session_binding(recovered['session']):
                    result = {'sessionId': recovered['session'], 'targetUri': recovered['target'],
                              'status': 'unconfirmed', 'created': False}
                else:
                    result = self.adapter.create_session_from(params) if hasattr(
                        self.adapter, 'create_session_from') else self.adapter.create_session(params.get('targetUri'))
                binding = self.adapter.session_binding(result['sessionId'])
                result = {**result, 'conversationId': binding.conversation_id}
            elif operation in ('sessions.send', 'sessions.send.lookup', 'sessions.send.confirm'):
                result = self._send(operation, params)
            else:
                return handle_request(self.adapter, frame)
            return {'ok': True, 'result': result}
        except (ValueError, TypeError, KeyError):
            return failure('invalid-argument', '请求参数无效或与原请求不一致。', definitive=True)
        except AppAdapterError as error:
            return failure(error.code, '请求被拒绝，请检查当前状态。', definitive=True)
        except (AppAdapterUnavailable, DaemonRequestError):
            return failure('unavailable', '结果尚未确认，请查询状态。')
        except Exception:
            # Credential-bearing vendor exceptions must not cross the bridge.
            return failure('unavailable', '服务暂不可用，结果尚未确认。')

    def _send(self, operation, params):
        session, request = params.get('sessionId'), params.get('request')
        if not isinstance(session, str) or not isinstance(request, dict):
            raise ValueError('session and frozen request required')
        if set(request) != {'text', 'idempotencyKey'} or any(
            not isinstance(value, str) or not value.strip() for value in request.values()
        ) or len(request['text'].encode()) > 65536 or len(request['idempotencyKey']) > 128:
            raise ValueError('invalid frozen request')
        binding = self.adapter.session_binding(session)
        if binding is None or len(binding.targets) != 1:
            raise ValueError('unknown session')
        target, conversation = binding.targets[0], binding.conversation_id
        sending = operation == 'sessions.send' and self.journal.begin(
            session, target, conversation, request)
        try:
            result = (self.adapter.send if sending else self.adapter.send_lookup)(session, request)
        except AppAdapterError as error:
            if not sending or (isinstance(error.details, dict) and error.details.get('acceptedTargets')):
                raise AppAdapterUnavailable('outcome-unknown') from error
            if sending:
                self.journal.rejected(request['idempotencyKey'])
            raise
        except Exception as error:
            # Errors after invoking the adapter cannot be turned into local
            # validation failures: a write may already have been accepted.
            raise AppAdapterUnavailable('outcome-unknown') from error
        if not isinstance(result, dict):
            raise AppAdapterUnavailable('invalid-response')
        if result.get('status') == 'accepted':
            if (result.get('commandId') != request['idempotencyKey'] or result.get('conversationId') != conversation
                    or not isinstance(result.get('messageId'), str) or not result['messageId']):
                raise AppAdapterUnavailable('invalid-response')
            # Receipt delivery to the browser is a separate boundary. Keep the
            # frozen request until that browser explicitly confirms receipt;
            # an HTTP disconnect after our success must survive refresh.
            if operation == 'sessions.send.confirm':
                self.journal.resolved(request['idempotencyKey'], session, target, conversation, request['text'])
        return result

    def close(self):
        if self.authorization:
            self.authorization.close()
