"""Internal journal invariants; synthetic receipts are NOT remote authority."""

from dataclasses import replace
import json

import pytest

from hyprial.daemon.impl.transfer.p2.model import AdmissionConflict, Binding, Reason
from hyprial.daemon.impl.transfer.p2.store import AdmissionStore


def start(store, binding):
    store.prepare("op", binding)
    return store.journal.begin("op", step="quiesce", fingerprint="b" * 64, actor="worker")


def test_identical_intent_never_reissues_execution_permission(ledger, binding):
    store, _, _ = ledger
    first = start(store, binding)
    assert first.execute
    for _ in range(2):
        again = store.journal.begin("op", step="quiesce", fingerprint="b" * 64, actor="worker")
        assert again.state == first.state and not again.execute
    finished = store.journal.finish("op", expected=first.state, receipt="receipt",
                                    entity_token=binding.source_entity_token)
    again = store.journal.begin("op", step="quiesce", fingerprint="b" * 64, actor="worker")
    assert again.state == finished and not again.execute


@pytest.mark.parametrize("field,value", [("fingerprint", "c" * 64), ("actor", "other")])
def test_different_intent_cannot_attach(ledger, binding, field, value):
    store, _, _ = ledger
    before = start(store, binding).state
    params = dict(step="quiesce", fingerprint="b" * 64, actor="worker")
    params[field] = value
    with pytest.raises(AdmissionConflict) as error:
        store.journal.begin("op", **params)
    assert error.value.reason is Reason.REPLAY_MISMATCH
    assert store.journal.get("op") == before


@pytest.mark.parametrize("field,value", [("generation", 99), ("fingerprint", "c" * 64),
                                         ("actor", "other"), ("operation_id", "other")])
def test_completion_must_match_exact_intent(ledger, binding, field, value):
    store, _, _ = ledger
    state = start(store, binding).state
    with pytest.raises(AdmissionConflict) as error:
        store.journal.finish("op", expected=replace(state, **{field: value}), receipt="r",
                             entity_token=binding.source_entity_token)
    assert error.value.reason is Reason.PHASE_CONFLICT
    assert store.journal.get("op") == state


def test_wrong_incarnation_cannot_record_source_completion(ledger, binding):
    store, _, _ = ledger
    state = start(store, binding).state
    with pytest.raises(AdmissionConflict) as error:
        store.journal.finish("op", expected=state, receipt="r", entity_token="replacement")
    assert error.value.reason is Reason.BINDING_MISMATCH
    assert store.journal.get("op") == state


def test_same_receipt_completion_is_idempotent_but_different_receipt_conflicts(ledger, binding):
    store, _, _ = ledger
    state = start(store, binding).state
    result = store.journal.finish("op", expected=state, receipt="r", entity_token=binding.source_entity_token)
    assert store.journal.finish("op", expected=state, receipt="r", entity_token=binding.source_entity_token) == result
    with pytest.raises(AdmissionConflict):
        store.journal.finish("op", expected=state, receipt="different", entity_token=binding.source_entity_token)


@pytest.mark.parametrize("change", ["expire", "revoke", "cancel"])
def test_nonlive_admission_cannot_begin_new_effect(ledger, binding, change):
    store, now, _ = ledger
    record = store.prepare("op", binding)
    if change == "expire":
        now[0] = record.expires_at_ms
    elif change == "revoke":
        store.revoke("op")
    else:
        store.cancel_prepared("op")
    with pytest.raises(AdmissionConflict):
        store.journal.begin("op", step="quiesce", fingerprint="b" * 64, actor="worker")
    assert store.journal.get("op") is None


def test_revoke_then_cancel_cannot_release_post_intent_custody(ledger, binding):
    store, _, _ = ledger
    start(store, binding)
    store.revoke("op")
    with pytest.raises(AdmissionConflict) as error:
        store.cancel_prepared("op")
    assert error.value.reason is Reason.PHASE_CONFLICT
    with pytest.raises(AdmissionConflict) as error:
        store.prepare("new", binding)
    assert error.value.reason is Reason.RESERVED


def test_expiry_does_not_discard_already_accepted_completion(ledger, binding):
    store, now, _ = ledger
    state = start(store, binding).state
    now[0] = store.get("op").expires_at_ms
    result = store.journal.finish("op", expected=state, receipt="r", entity_token=binding.source_entity_token)
    assert result.phase == "quiesced"
    assert store.journal.custody("worker", binding.source_entity_token).blocked


def test_target_requires_landed_before_activate_and_preserves_token(ledger, target_binding):
    _, now, db = ledger
    store = AdmissionStore(db, node="target", owner="test-owner", clock_ms=lambda: now[0])
    store.prepare("op", target_binding)
    with pytest.raises(AdmissionConflict):
        store.journal.begin("op", step="activate", fingerprint="d" * 64, actor="visitor")
    first = store.journal.begin("op", step="land", fingerprint="c" * 64, actor="visitor")
    with pytest.raises(AdmissionConflict):
        store.journal.begin("op", step="activate", fingerprint="d" * 64, actor="visitor")
    store.journal.finish("op", expected=first.state, receipt="land", entity_token="new-target")
    second = store.journal.begin("op", step="activate", fingerprint="d" * 64, actor="visitor")
    assert second.state.generation == first.state.generation + 1
    history = store.journal.history("op")
    assert [entry.phase for entry in history] == ["landed", "activate-pending"]
    assert history[0].receipt == "land" and history[0].entity_token == "new-target"
    with pytest.raises(AdmissionConflict):
        store.journal.finish("op", expected=second.state, receipt="active", entity_token="replacement")
    assert store.journal.finish("op", expected=second.state, receipt="active",
                                entity_token="new-target").phase == "active"


@pytest.mark.parametrize("step", ["land", "activate"])
def test_source_cannot_execute_target_steps(ledger, binding, step):
    store, _, _ = ledger
    store.prepare("op", binding)
    with pytest.raises(AdmissionConflict):
        store.journal.begin("op", step=step, fingerprint="b" * 64, actor="worker")
    assert store.journal.get("op") is None


def test_target_actor_mismatch_rejects_without_intent(ledger, target_binding):
    _, now, db = ledger
    store = AdmissionStore(db, node="target", owner="test-owner", clock_ms=lambda: now[0])
    store.prepare("op", target_binding)
    with pytest.raises(AdmissionConflict):
        store.journal.begin("op", step="land", fingerprint="b" * 64, actor="other")
    assert store.journal.get("op") is None


def test_attention_event_replays_but_cannot_rebind_and_never_unlocks(ledger, binding):
    store, _, _ = ledger
    state = start(store, binding).state
    kwargs = dict(expected=state, event_id="override", principal="test-owner", action="manual-restore")
    result = store.journal.attention("op", **kwargs)
    assert store.journal.attention("op", **kwargs) == result
    assert len(store.journal.events("op")) == 1
    with pytest.raises(AdmissionConflict):
        store.journal.attention("op", **dict(kwargs, action="other"))
    with pytest.raises(AdmissionConflict):
        store.journal.finish("op", expected=state, receipt="r", entity_token=binding.source_entity_token)
    assert store.journal.custody("worker", binding.source_entity_token).blocked


def test_owner_rename_is_a_query_failure_not_absent(ledger, binding):
    store, now, db = ledger
    start(store, binding)
    changed = AdmissionStore(db, node="source", owner="renamed", clock_ms=lambda: now[0])
    with pytest.raises(AdmissionConflict) as error:
        changed.journal.custody("worker", binding.source_entity_token)
    assert error.value.reason is Reason.BINDING_MISMATCH


def test_corrupt_phase_is_not_absent_and_cannot_be_cancelled(ledger, binding):
    store, _, db = ledger
    start(store, binding)
    with db.transaction() as conn:
        conn.execute("UPDATE transfer_p2_steps SET phase='bogus',receipt='valid-receipt'")
    for read in (lambda: store.journal.get("op"),
                 lambda: store.journal.custody("worker", binding.source_entity_token)):
        with pytest.raises(AdmissionConflict) as error:
            read()
        assert error.value.reason is Reason.CORRUPT_RECORD
    with pytest.raises(AdmissionConflict):
        store.cancel_prepared("op")


@pytest.mark.parametrize("raw", ['[]', '{"unknown":true}', '{', '{"_schema_version":99}'])
def test_binding_decode_corruption_is_a_domain_error(raw):
    with pytest.raises(AdmissionConflict) as error:
        Binding.decode(raw)
    assert error.value.reason is Reason.CORRUPT_RECORD


def test_old_binding_encoding_still_reads_and_new_version_is_explicit(binding):
    raw = json.loads(binding.encode())
    assert raw.pop("_schema_version") == 1
    assert Binding.decode(json.dumps(raw)) == binding
    assert Binding.decode(binding.encode()) == binding


def test_valid_binding_with_unknown_schema_is_rejected(binding):
    raw = json.loads(binding.encode())
    raw["_schema_version"] = 99
    with pytest.raises(AdmissionConflict) as error:
        Binding.decode(json.dumps(raw))
    assert error.value.reason is Reason.CORRUPT_RECORD


def test_unknown_phase_schema_is_not_absent(ledger, binding):
    store, _, db = ledger
    start(store, binding)
    with db.transaction() as conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE transfer_p2_steps SET schema_version=2")
    with pytest.raises(AdmissionConflict) as error:
        store.journal.custody("worker", binding.source_entity_token)
    assert error.value.reason is Reason.CORRUPT_RECORD
