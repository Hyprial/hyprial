"""C entry contract: explicit assertions before any new API is invoked.

The availability assertion makes old-source failure a predicted missing
behavior, never a child import failure or a broken subprocess harness.
"""

import pytest

from hyprial.daemon.impl.transfer.p2.model import AdmissionConflict


def journal_port(store):
    port = getattr(store, "journal", None)
    assert port is not None, "P2 lacks durable phase/intent authority"
    return port


def test_source_intent_blocks_preparation_cancel_and_survives_reopen(ledger, binding):
    from hyprial.daemon.impl.transfer.p2.store import AdmissionStore

    store, now, db = ledger
    store.prepare("op", binding)
    journal = journal_port(store)
    intent = journal.begin("op", step="quiesce", fingerprint="b" * 64, actor="worker").state
    with pytest.raises(AdmissionConflict, match="phase-conflict"):
        store.cancel_prepared("op")
    restored = AdmissionStore(db, node="source", owner="test-owner", clock_ms=lambda: now[0])
    assert restored.journal.get("op") == intent
    assert restored.journal.custody("worker", binding.source_entity_token).blocked
    assert not restored.journal.custody("worker", "replacement").blocked


def test_target_land_is_not_active_after_reopen(ledger, target_binding):
    from hyprial.daemon.impl.transfer.p2.store import AdmissionStore

    _, now, db = ledger
    store = AdmissionStore(db, node="target", owner="test-owner", clock_ms=lambda: now[0])
    store.prepare("op", target_binding)
    journal = journal_port(store)
    intent = journal.begin("op", step="land", fingerprint="c" * 64, actor="visitor").state
    landed = journal.finish("op", expected=intent, receipt="land-receipt", entity_token="target-token")
    assert landed.phase == "landed"
    reopened = AdmissionStore(db, node="target", owner="test-owner", clock_ms=lambda: now[0])
    assert reopened.journal.get("op").phase == "landed"
    assert reopened.journal.get("op").phase != "active"


def test_override_records_attention_without_unlocking(ledger, binding):
    store, _, _ = ledger
    store.prepare("op", binding)
    journal = journal_port(store)
    intent = journal.begin("op", step="quiesce", fingerprint="b" * 64, actor="worker").state
    journal.attention("op", expected=intent, event_id="override-1", principal="test-operator", action="manual-restore")
    state = journal.get("op")
    assert state.needs_attention
    assert state.phase == "quiesce-pending"
    assert journal.custody("worker", binding.source_entity_token).blocked
    assert journal.events("op")[-1].principal == "test-operator"
    assert journal.events("op")[-1].action == "manual-restore"
