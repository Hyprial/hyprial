"""Session owner consumes incarnation fences before any persistence effect."""

import inspect
import threading
from dataclasses import replace
from queue import Queue

import pytest

from hyprial.kernel import LifecycleMutationCompleted, LifecycleMutationRequest
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.operations.session_ports import RegisterSessionCommand, UnregisterSessionCommand
from hyprial.daemon.impl.session_actor import SessionActor


@pytest.mark.parametrize('token', ['old', 'current', None, 'unavailable'])
def test_session_owner_checks_incarnation_before_removal(tmp_path, token):
    assert 'agent_identity' in inspect.signature(SessionActor).parameters, \
        'session owner lacks incarnation projection port'
    reads = []

    class Identity:
        def entity_token(self, actor):
            assert actor == 'source'
            reads.append(threading.get_ident())
            if token == 'unavailable':
                raise RuntimeError('projection unavailable')
            return 'current'

    store = DesiredStateStore(tmp_path/'desired.json')
    create = LifecycleMutationRequest('create', 'create-attempt', 'create-op', None,
        RegisterSessionCommand('create', 'source', str(tmp_path), ('pi',), 'test', 'session'))
    store.apply_session_lifecycle(create)
    events = Queue()
    owner = SessionActor(store, daemon_epoch='test', event_sink=events.put, agent_identity=Identity())
    command = LifecycleMutationRequest('remove', 'remove-attempt', 'remove-op', None,
        UnregisterSessionCommand('remove', 'source', 'session'), expected_entity_token=token)
    try:
        owner.submit(command)
        event = events.get(timeout=5)
        if token in {'old', 'unavailable'}:
            expected = 'AGENT_INCARNATION_MISMATCH' if token == 'old' else 'TRANSFER_CUSTODY_UNAVAILABLE'
            assert getattr(event, 'code', None) == expected
            assert len(store.load().interactive_sessions) == 1
            assert len(store.load().lifecycle_receipts) == 1
        else:
            assert isinstance(event, LifecycleMutationCompleted)
            assert not store.load().interactive_sessions
            reopened = DesiredStateStore(tmp_path/'desired.json')
            row = next(r for r in reopened.load().lifecycle_receipts if r.attempt_token == 'remove-attempt')
            assert row.expected_entity_token == token
            with pytest.raises(ValueError, match='attempt token was reused'):
                reopened.apply_session_lifecycle(replace(command, expected_entity_token='different'))
        if token is not None:
            assert reads and all(t != threading.get_ident() for t in reads)
    finally:
        assert owner.drain(5).complete
