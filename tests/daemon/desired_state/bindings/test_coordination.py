"""Cross-store recovery retains compensation intent and a durable cancellation choice."""

from dataclasses import replace
import json

import pytest

from hyprial.daemon.impl.desired_state.store import DesiredStateStore
from hyprial.daemon.impl.desired_state.bindings.models import Configuration
from hyprial.kernel import DesiredStateError
from .test_transactions import prepare


def plan():
    return {
        "schemaVersion": 1,
        "cellId": "cell.fixture",
        "expectedRevision": 4,
        "previousIntent": {"kind": "claude"},
        "phase": "planned",
        "observedRevision": None,
    }


def advance(store, operation, **changes):
    return store.model_binding_store.checkpoint(
        operation.agent_id, operation.member_id, operation.operation_id,
        operation.request_digest, operation.expected_revision,
        operation.coordination.to_json(),
        {**operation.coordination.to_json(), **changes},
    )


def test_first_configuration_preserves_missing_old_model_across_restart(tmp_path):
    store = DesiredStateStore(tmp_path / "desired.json")
    first = prepare(store, coordination=plan())
    restarted = DesiredStateStore(store.legacy_path)
    assert restarted.model_binding_store.status("a.fixture", "member-1").binding is None
    pending = restarted.model_binding_store.status("a.fixture", "member-1").pending
    assert pending.coordination.to_json()["previousIntent"] == {"kind": "claude"}
    payload = restarted.load().to_json()
    assert type(restarted.load()).from_json(payload).model_binding_operations == (first,)
    restarted.save(replace(restarted.load(), as_mailbox=True))
    assert restarted.load().model_binding_operations == (first,)
    with store.state_db.read() as db:
        encoded = json.loads(db.execute("SELECT candidate_json FROM model_binding_operations").fetchone()[0])
    with pytest.raises(DesiredStateError):
        Configuration.from_json(encoded)


def test_cancel_choice_survives_restart_and_refuses_late_finalize(tmp_path):
    store = DesiredStateStore(tmp_path / "desired.json")
    first = prepare(store, coordination=plan())
    applied = advance(store, first, phase="applied", observedRevision=5)
    cancelling = advance(store, applied, phase="cancelling")
    restarted = DesiredStateStore(store.legacy_path)
    args = (first.agent_id, first.member_id, first.operation_id, first.request_digest, 0)
    with pytest.raises(DesiredStateError, match="operation-in-progress"):
        restarted.model_binding_store.finalize(*args)
    with pytest.raises(DesiredStateError, match="operation-in-progress"):
        restarted.model_binding_store.cancel(*args)
    with pytest.raises(DesiredStateError, match="revision-mismatch"):
        advance(restarted, applied, phase="applied", observedRevision=5)
    compensated = advance(restarted, cancelling, phase="compensated", observedRevision=6)
    result = restarted.model_binding_store.cancel(*args)
    assert result.phase == "cancelled" and result.result_revision == 0
    assert result.coordination == compensated.coordination
    assert prepare(restarted, coordination=plan()) == result
    assert restarted.model_binding_store.status(first.agent_id, first.member_id).binding is None


def test_cell_revision_and_compensation_intent_cannot_be_replaced_on_retry(tmp_path):
    store = DesiredStateStore(tmp_path / "desired.json")
    operation = prepare(store, coordination=plan())
    for change in ({"expectedRevision": 5}, {"previousIntent": {"model": "other"}}):
        with pytest.raises(DesiredStateError, match="operation-digest-mismatch"):
            prepare(store, coordination={**plan(), **change})
        with pytest.raises(DesiredStateError, match="invalid-connection"):
            advance(store, operation, **change)
    args = (operation.agent_id, operation.member_id, operation.operation_id, operation.request_digest, 0)
    with pytest.raises(DesiredStateError, match="operation-in-progress"):
        store.model_binding_store.finalize(*args)


def test_finalize_requires_persisted_cell_confirmation(tmp_path):
    store = DesiredStateStore(tmp_path / "desired.json")
    first = prepare(store, coordination=plan())
    applied = advance(store, first, phase="applied", observedRevision=5)
    args = (first.agent_id, first.member_id, first.operation_id, first.request_digest, 0)
    result = store.model_binding_store.finalize(*args)
    assert result.phase == "finalized"
    assert result.coordination == applied.coordination
    assert store.model_binding_store.finalize(*args) == result
    with pytest.raises(DesiredStateError, match="operation-already-completed"):
        advance(store, applied, phase="cancelling")
