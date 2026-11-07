"""Long source effects cannot occupy the journal mailbox or lose observers."""

import importlib.util
from dataclasses import replace
import threading
from concurrent.futures import Future

import pytest
from hyprial.kernel import EffectCompleted

from .test_effect import build


def runner(effect):
    path = 'hyprial.daemon.impl.transfer.p2.source.runner'
    assert importlib.util.find_spec(path) is not None, 'quiesce lacks a separate effect runner'
    module = __import__(path, fromlist=['SourceRunner'])
    return module.SourceRunner(effect)


def test_same_request_attaches_and_different_request_is_rejected(ledger, binding):
    store, journal, holds, lifecycle, request, effect = build(ledger, binding)
    owner = None
    try:
        owner = runner(effect)
        first = owner.submit(request)
        assert holds.entered.wait(5)
        second = owner.submit(request)
        changed = replace(request, spec=replace(request.spec, interruption_reason='different'))
        with pytest.raises(ValueError, match='fingerprint'):
            owner.submit(changed).result(5)
        assert not second.done()
        assert first.cancel()
        assert not owner.close(timeout=0), 'closing must preserve accepted effect custody'
        holds.release.set()
        assert second.result(5).state == 'quiesced'
        assert holds.calls == 1 and len(lifecycle.calls) == 1
        assert store.journal.get('move').phase == 'quiesced'
        assert owner.close(timeout=5)
    finally:
        holds.release.set()
        if owner is not None:
            owner.close(timeout=5)
        assert journal.close(timeout=5)


def test_duplicate_old_completion_cannot_settle_next_submission(ledger, binding):
    _, journal, holds, _, request, effect = build(ledger, binding)
    entered, release = threading.Event(), threading.Event()
    executions = []

    def execute(value):
        executions.append(value)
        if len(executions) == 2:
            entered.set()
            assert release.wait(5)
        return effect(value)

    owner = None
    holds.release.set()
    try:
        owner = runner(execute)
        first = owner.submit(request).result(5)
        second = owner.submit(request)
        assert entered.wait(5)
        owner._runtime.tell(owner._handle, EffectCompleted(request.operation_id, 1, first))
        # Drain is ordered after the duplicate. It must not report complete
        # while the second execution is deliberately held behind a barrier.
        from hyprial.daemon.impl.transfer.p2.source.runner import _Drain
        drained = Future()
        owner._runtime.tell(owner._handle, _Drain(drained))
        with pytest.raises(TimeoutError):
            drained.result(0.1)
        assert not second.done(), 'old completion acknowledged a newer effect'
        release.set()
        assert second.result(5).state == 'quiesced'
    finally:
        release.set()
        if owner is not None:
            owner.close(timeout=5)
        assert journal.close(timeout=5)
