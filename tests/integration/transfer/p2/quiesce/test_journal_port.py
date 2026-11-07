"""Short journal effects publish custody without blocking the P2 mailbox."""

from dataclasses import replace
import threading

import pytest

from hyprial.daemon.impl.transfer.p2 import actor as module
from hyprial.daemon.impl.transfer.p2.model import AdmissionConflict, Reason


def intent(binding):
    cls = getattr(module, 'SourceIntent', None)
    assert cls is not None, 'P2 source intent has no typed authority command'
    return cls('move', 'source', binding.source_entity_token, binding.source_agent_id, 'fingerprint')


def test_source_journal_commands_run_in_short_lane_and_publish_custody(ledger, binding, monkeypatch):
    command = intent(binding)
    store, _, _ = ledger
    store.prepare('move', binding)
    entered, release = threading.Event(), threading.Event()
    begin = store.journal.begin
    threads = []

    def blocked(*args, **kwargs):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return begin(*args, **kwargs)

    monkeypatch.setattr(store.journal, 'begin', blocked)
    owner = module.AdmissionActor(store)
    try:
        owner.hydrate_custody('source', expected_entity_token=binding.source_entity_token).result(5)
        result = owner.submit(command)
        assert entered.wait(5)
        # Pure memory status stays responsive while the SQLite effect waits.
        assert owner.source_custody('source', expected_entity_token=binding.source_entity_token).state == 'unavailable'
        release.set()
        started = result.result(5)
        assert started.execute and started.state.phase == 'quiesce-pending'
        assert not owner.submit(command).result(5).execute
        cls = getattr(module, 'SourceFinish', None)
        assert cls is not None, 'P2 completion has no typed authority command'
        completed = owner.submit(cls(started.state, 'lifecycle:move:1')).result(5)
        assert completed.phase == 'quiesced'
        assert owner.source_custody('source', expected_entity_token=binding.source_entity_token).state == 'held'
        assert all(t != threading.get_ident() for t in threads)
    finally:
        release.set()
        assert owner.close(timeout=5)


@pytest.mark.parametrize('field', ['expected_entity_token', 'source_agent_id'])
def test_source_intent_rejects_changed_binding_before_journal(ledger, binding, field):
    command = replace(intent(binding), **{field: 'wrong'})
    store, _, _ = ledger
    store.prepare('move', binding)
    owner = module.AdmissionActor(store)
    try:
        with pytest.raises(AdmissionConflict) as error:
            owner.submit(command).result(5)
        assert error.value.reason is Reason.BINDING_MISMATCH
        assert store.journal.get('move') is None
    finally:
        assert owner.close(timeout=5)
