"""Quiesce ordering with real journal and deterministic downstream barriers."""

import importlib.util
import inspect
import json
from pathlib import Path
from dataclasses import replace
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.transfer.p2.actor import AdmissionActor
from hyprial.daemon.impl.harnesses.runtime.transfer_ports import StartsHeld
from hyprial.daemon.impl.lifecycle_manager import LifecycleResult, LifecycleState
from tests.daemon.lifecycle.process.manager.support import _spec


def module():
    path = 'hyprial.daemon.impl.transfer.p2.source.effect'
    assert importlib.util.find_spec('hyprial.daemon.impl.transfer.p2.source') is not None, \
        'source quiesce effect is missing'
    return __import__(path, fromlist=['QuiesceEffect'])


class Holds:
    def __init__(self, store):
        self.store = store
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = 0
        self.disposition = 'held'
        self.wrong_receipt = False

    def call(self, command, expected, *, timeout):
        assert self.store.journal.get('move').phase == 'quiesce-pending'
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout)
        return StartsHeld('unrelated' if self.wrong_receipt else command.correlation_id,
                          command.actor, command.expected_entity_token,
                          command.operation_id, command.generation, self.disposition,
                          version=1)


class Lifecycle:
    def __init__(self):
        self.calls = []
        self.timeout = False
        self.state = LifecycleState.COMPLETED
        self.wrong_receipt = False
        self.admission = PortAdmission.ACCEPTED

    def submit(self, operation):
        self.calls.append(operation)
        return self.admission

    def wait(self, operation_id, timeout):
        if self.timeout:
            raise TimeoutError('still owned by lifecycle')
        return LifecycleResult('unrelated' if self.wrong_receipt else operation_id, self.state, (), ())


def build(ledger, binding):
    api = module()
    store, _, _ = ledger
    store.prepare('move', binding)
    owner = AdmissionActor(store)
    holds, lifecycle = Holds(store), Lifecycle()
    request = api.QuiesceRequest('move', binding.source_agent_id,
                                replace(_spec(), expected_entity_token=binding.source_entity_token))
    effect = api.QuiesceEffect(owner, holds, lifecycle)
    return store, owner, holds, lifecycle, request, effect


def test_intent_precedes_hold_and_hold_ack_precedes_deactivate(ledger, binding):
    store, owner, holds, lifecycle, request, effect = build(ledger, binding)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(effect, request)
            try:
                assert holds.entered.wait(5)
                assert lifecycle.calls == []
                assert owner.source_custody(request.spec.actor,
                    expected_entity_token=binding.source_entity_token).state == 'unavailable'
            finally:
                holds.release.set()
            result = future.result(5)
        assert result.state == 'quiesced'
        assert store.journal.get('move').actor == request.spec.agent_name
        assert owner.source_custody(request.spec.agent_name,
            expected_entity_token=binding.source_entity_token).state == 'held'
        assert len(lifecycle.calls) == 1
        operation = lifecycle.calls[0]
        assert operation.operation_id == 'transfer-quiesce:move:1'
        assert operation.kind.value == 'deactivate'
        assert operation.target.expected_entity_token == binding.source_entity_token
        assert store.journal.get('move').receipt == operation.operation_id
        assert effect(request).state == 'quiesced'
        assert holds.calls == 1 and len(lifecycle.calls) == 1
    finally:
        assert owner.close(timeout=5)


@pytest.mark.parametrize('failure', ['hold-mismatch', 'hold-wrong-receipt', 'lifecycle-timeout',
                                    'lifecycle-failed', 'lifecycle-wrong-receipt', 'not-admitted'])
def test_unproven_stop_never_finishes_or_reexecutes_pending_intent(ledger, binding, failure):
    store, owner, holds, lifecycle, request, effect = build(ledger, binding)
    holds.release.set()
    holds.disposition = 'entity-mismatch' if failure == 'hold-mismatch' else 'held'
    lifecycle.timeout = failure == 'lifecycle-timeout'
    holds.wrong_receipt = failure == 'hold-wrong-receipt'
    lifecycle.wrong_receipt = failure == 'lifecycle-wrong-receipt'
    if failure == 'lifecycle-failed':
        lifecycle.state = LifecycleState.FAILED
    if failure == 'not-admitted':
        lifecycle.admission = PortAdmission.OVERLOADED
    try:
        result = effect(request)
        assert result.state == 'unknown'
        assert store.journal.get('move').phase == 'quiesce-pending'
        assert len(lifecycle.calls) == (0 if failure.startswith('hold-') else 1)
        assert effect(request).state == 'unknown'
        assert holds.calls == 1
        assert len(lifecycle.calls) == (0 if failure.startswith('hold-') else 1)
    finally:
        assert owner.close(timeout=5)


def test_quiesce_budgets_and_identity_consistency_are_explicit():
    api = module()
    from hyprial.daemon.impl.transfer.p2.source.runner import SourceRunner
    root = Path(__file__).resolve().parents[5]
    bounds = json.loads((root/'contract/daemon-lifecycle/p2-source-bounds.json').read_text())
    assert inspect.signature(api.QuiesceEffect).parameters['deadline_seconds'].default == bounds['source_quiesce_deadline_seconds']
    assert inspect.signature(SourceRunner).parameters['capacity'].default == bounds['source_effect_capacity']
    # The caller-side waits the P2 quiesce path adds are declared in the same
    # file and derived, not copied: the deadline is the effect's budget, the
    # wait outlasts it, and the admission wait is the custody query budget.
    from hyprial.daemon.impl.application.actors.transfer import (
        P2_ADMISSION_WAIT_SECONDS,
        P2_QUIESCE_WAIT_SECONDS,
    )
    assert P2_QUIESCE_WAIT_SECONDS == bounds['p2_quiesce_wait_seconds']
    assert P2_QUIESCE_WAIT_SECONDS == bounds['source_quiesce_deadline_seconds'] + 30
    assert P2_ADMISSION_WAIT_SECONDS == bounds['p2_admission_wait_seconds']
    assert P2_ADMISSION_WAIT_SECONDS == bounds['source_custody_query_seconds']
    with pytest.raises(ValueError, match='identities'):
        api.QuiesceRequest('op', 'agent-id', replace(_spec(), actor='other', expected_entity_token='current'))
