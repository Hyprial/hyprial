"""Incarnation is independent from the resource compensation receipt."""

from dataclasses import fields, replace
from queue import Queue
from contextlib import closing
import sqlite3
import threading

import pytest

from hyprial.identity import AgentActor, AgentRegistry, BindAgentCommand
from hyprial.kernel import LifecycleMutationRequest, LifecycleMutationCompleted
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation, LifecycleSpec
from hyprial.daemon.impl.lifecycle_manager.steps import _operation_json, _operation_from_json
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessLaunchProjection
from hyprial.daemon.impl.network.route_ports import RouteSpec


def request(payload, token=None, resource=None):
    assert 'expected_entity_token' in {f.name for f in fields(LifecycleMutationRequest)}, \
        'lifecycle envelope is missing its independent incarnation fence'
    return LifecycleMutationRequest('call', 'attempt', 'operation', resource,
                                    payload, expected_entity_token=token)


@pytest.mark.parametrize('token', [None, 'entity-current'])
def test_envelope_preserves_separate_entity_and_resource_tokens(token):
    command = request(object(), token, 'resource-custody')
    assert command.expected_resource_token == 'resource-custody'
    assert command.expected_entity_token == token


@pytest.mark.parametrize('token', ['', '  '])
def test_envelope_rejects_blank_entity_token(token):
    with pytest.raises(ValueError, match='expected_entity_token'):
        request(object(), token)


def test_lifecycle_spec_roundtrip_preserves_entity_fence():
    assert 'expected_entity_token' in {f.name for f in fields(LifecycleSpec)}, \
        'lifecycle journal spec discards the incarnation fence'
    spec = LifecycleSpec('source', 'source', HarnessLaunchProjection('pi', 'source', True),
                         RouteSpec('source', 'live/source', 'inbox/source'),
                         expected_entity_token='entity-current')
    operation = LifecycleOperation.deactivate('op', spec)
    assert _operation_from_json(_operation_json(operation)) == operation
    legacy = replace(operation, target=replace(spec, expected_entity_token=None))
    assert 'expectedEntityToken' not in _operation_json(legacy)
    assert _operation_from_json(_operation_json(legacy)) == legacy


@pytest.mark.parametrize('mode', ['wrong', 'current', 'legacy'])
def test_identity_owner_consumes_entity_fence(tmp_path, monkeypatch, mode):
    registry = AgentRegistry(tmp_path/'agents.sqlite3', owner='test-owner', machine='test-node')
    agent = registry.create('source')
    events = Queue()
    threads = []
    original = registry.apply_lifecycle

    def apply(command):
        threads.append(threading.get_ident())
        return original(command)

    monkeypatch.setattr(registry, 'apply_lifecycle', apply)
    class Sink:
        def publish(self, event):
            events.put(event)
    owner = AgentActor(registry, event_sink=Sink())
    try:
        token = {'wrong': 'stale', 'current': agent.entity_token, 'legacy': None}[mode]
        owner.submit(request(BindAgentCommand('call', agent.uri, 'pi', 'headless'), token))
        event = events.get(timeout=5)
        if mode == 'wrong':
            assert getattr(event, 'code', None) == 'AGENT_INCARNATION_MISMATCH'
            assert owner.read_binding(agent.uri) is None
        else:
            assert isinstance(event, LifecycleMutationCompleted)
            assert owner.read_binding(agent.uri) is not None
        assert threads and all(t != threading.get_ident() for t in threads)
    finally:
        assert owner.drain(5).complete
        registry.close()


def test_receipt_rejects_replay_with_changed_fence(tmp_path):
    registry = AgentRegistry(tmp_path/'agents.sqlite3', owner='test-owner', machine='test-node')
    try:
        agent = registry.create('source')
        command = request(BindAgentCommand('call', agent.uri, 'pi', 'headless'))
        registry.apply_lifecycle(command)
        # Correct current token cannot relabel a legacy receipt as fenced.
        with pytest.raises(ValueError, match='attempt token was reused'):
            registry.apply_lifecycle(replace(command, expected_entity_token=agent.entity_token))
    finally:
        registry.close()


def test_old_receipt_schema_upgrades_without_losing_replay(tmp_path):
    path = tmp_path/'agents.sqlite3'
    registry = AgentRegistry(path, owner='test-owner', machine='test-node')
    try:
        agent = registry.create('source')
        command = request(BindAgentCommand('call', agent.uri, 'pi', 'headless'))
        first = registry.apply_lifecycle(command)
    finally:
        registry.close()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('ALTER TABLE lifecycle_receipts DROP COLUMN expected_entity_token')
    reopened = AgentRegistry(path, owner='test-owner', machine='test-node')
    try:
        replay = reopened.apply_lifecycle(command)
        assert replay[0] == first[0]
        assert replay[2] is True
    finally:
        reopened.close()
