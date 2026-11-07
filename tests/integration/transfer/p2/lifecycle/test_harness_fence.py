"""Actual harness owner admission and persistent receipt incarnation binding."""

from dataclasses import replace
from queue import Queue
import threading

import pytest

from hyprial.kernel import LifecycleMutationCompleted, LifecycleMutationRequest
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.actor.facade import HarnessRuntimeFacade
from hyprial.daemon.impl.harnesses.runtime.ports import (
    EnsureHarnessCommand, HarnessLaunchProjection, RemoveHarnessCommand,
)
from ..source.hold_support import Launcher


def command(token):
    return LifecycleMutationRequest('call', 'attempt', 'operation', None,
                                    RemoveHarnessCommand('call', 'pi', 'source'),
                                    expected_entity_token=token)


@pytest.mark.parametrize('token', ['old', 'current', None])
def test_harness_owner_checks_incarnation_before_removal(tmp_path, token):
    reads = []
    class Identity:
        def entity_token(self, actor):
            assert actor == 'source'
            reads.append(threading.get_ident())
            return 'current'
    store = DesiredStateStore(tmp_path/'desired.json')
    launcher = Launcher()
    owner = HarnessRuntimeActor(launcher, desired_state=store, agent_identity=Identity())
    runtime = HarnessRuntimeFacade(owner)
    events = Queue()
    owner.subscribe_events(events.put)
    try:
        owner.submit(LifecycleMutationRequest(
            'create', 'create-attempt', 'create-op', None,
            EnsureHarnessCommand('create', HarnessLaunchProjection('pi', 'source', True)),
        ))
        while True:
            created = events.get(timeout=5)
            if created.correlation_id == 'create':
                break
        assert isinstance(created, LifecycleMutationCompleted)
        assert launcher.processes[0].running
        owner.submit(command(token))
        while True:
            event = events.get(timeout=5)
            if event.correlation_id == 'call':
                break
        if token == 'old':
            assert getattr(event, 'code', None) == 'AGENT_INCARNATION_MISMATCH'
            assert not any(r.attempt_token == 'attempt' for r in store.load().lifecycle_receipts)
            assert launcher.processes[0].running
        else:
            assert isinstance(event, LifecycleMutationCompleted)
            assert not launcher.processes[0].running
        if token is not None:
            assert reads and all(t != threading.get_ident() for t in reads)
    finally:
        runtime.stop(5)


def test_harness_receipt_keeps_fence_across_store_reopen(tmp_path):
    path = tmp_path/'desired.json'
    store = DesiredStateStore(path)
    original = command('current')
    store.apply_harness_lifecycle(original)
    # Reopen SQLite-backed storage; in-memory command equality is insufficient.
    reopened = DesiredStateStore(path)
    row = reopened.load().lifecycle_receipts[0]
    assert getattr(row, 'expected_entity_token', None) == 'current'
    with pytest.raises(ValueError, match='attempt token was reused'):
        reopened.apply_harness_lifecycle(replace(original, expected_entity_token=None))


def test_harness_rechecks_incarnation_after_pending_persistence(tmp_path, monkeypatch):
    class Identity:
        token = 'current'
        def entity_token(self, actor):
            return self.token
    identity, launcher = Identity(), Launcher()
    store = DesiredStateStore(tmp_path/'desired.json')
    entered, release = threading.Event(), threading.Event()
    apply = store.apply_harness_lifecycle

    def blocked(request, **kwargs):
        if request.attempt_token == 'attempt':
            entered.set()
            assert release.wait(5)
        return apply(request, **kwargs)

    monkeypatch.setattr(store, 'apply_harness_lifecycle', blocked)
    owner = HarnessRuntimeActor(launcher, desired_state=store, agent_identity=identity)
    runtime = HarnessRuntimeFacade(owner)
    events = Queue()
    owner.subscribe_events(events.put)
    try:
        owner.submit(LifecycleMutationRequest(
            'create', 'create-attempt', 'create-op', None,
            EnsureHarnessCommand('create', HarnessLaunchProjection('pi', 'source', True))))
        while events.get(timeout=5).correlation_id != 'create':
            pass
        assert launcher.processes[0].running
        owner.submit(command('current'))
        assert entered.wait(5)
        identity.token = 'replacement'
        release.set()
        while True:
            event = events.get(timeout=5)
            if event.correlation_id == 'call':
                break
        assert getattr(event, 'code', None) == 'AGENT_INCARNATION_MISMATCH'
        assert launcher.processes[0].running
    finally:
        release.set()
        runtime.stop(5)
