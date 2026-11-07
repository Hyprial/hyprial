"""AT07 I3: the six crash windows of one P2 chain, decided from both ledgers.

Each window is built by driving the *real* source and target ledgers to the
state a crash would leave behind, then asking ``evaluate_chain`` what the
source side may do.  The probe is injected because source liveness is an owner
observation, never something this line may assume.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.daemon.impl.state_db import StateDatabase
from hyprial.daemon.impl.transfer.p2.model import AdmissionConflict, Binding, Role
from hyprial.daemon.impl.transfer.p2.orchestration.chain import evaluate_chain
from hyprial.daemon.impl.transfer.p2.store import AdmissionStore

OWNER = "alice"
SOURCE_NODE = "laptop"
TARGET_NODE = "server"
ACTOR = "wk"
OPERATION = "op-1"
PLAN_DIGEST = "a" * 64
QUIESCE_FINGERPRINT = "f" * 64
RECEIPT = "lifecycle:deactivated"


def _source_binding() -> Binding:
    return Binding(
        role=Role.SOURCE,
        owner=OWNER,
        source_node=SOURCE_NODE,
        source_agent_id=ACTOR,
        source_entity_token="source-incarnation",
        target_node=TARGET_NODE,
        target_actor=ACTOR,
        plan_digest=PLAN_DIGEST,
    )


def _target_binding() -> Binding:
    return replace(_source_binding(), role=Role.TARGET)


def _stores(tmp_path: Path) -> tuple[AdmissionStore, AdmissionStore]:
    source = AdmissionStore(
        StateDatabase(tmp_path / "source.sqlite3"),
        node=SOURCE_NODE,
        owner=OWNER,
        clock_ms=lambda: 1000,
    )
    target = AdmissionStore(
        StateDatabase(tmp_path / "target.sqlite3"),
        node=TARGET_NODE,
        owner=OWNER,
        clock_ms=lambda: 1000,
    )
    return source, target


def _rows(db: StateDatabase) -> list[tuple[object, ...]]:
    with db.read() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT operation_id, generation, phase, step, receipt, needs_attention "
                "FROM transfer_p2_steps ORDER BY operation_id, generation"
            )
        ]


def _hold_quiesce(store: AdmissionStore, *, finish: bool = False, attention: bool = False):
    store.prepare(OPERATION, _source_binding())
    start = store.journal.begin(
        OPERATION, step="quiesce", fingerprint=QUIESCE_FINGERPRINT, actor=ACTOR
    )
    state = start.state
    if finish:
        state = store.journal.finish(
            OPERATION, expected=state, receipt=RECEIPT, entity_token="source-incarnation"
        )
    if attention:
        store.journal.attention(
            OPERATION,
            expected=state,
            event_id="override-1",
            principal="operator",
            action="manual-restore",
        )
        state = store.journal.get(OPERATION)
    return state


def _land(store: AdmissionStore, *, finish: bool = True, activate: bool = False):
    store.prepare(OPERATION, _target_binding())
    start = store.journal.begin(
        OPERATION, step="land", fingerprint=PLAN_DIGEST, actor=ACTOR
    )
    if not finish:
        return start.state
    landed = store.journal.finish(
        OPERATION, expected=start.state, receipt="landed:wk:3:128:1", entity_token="target-token"
    )
    if not activate:
        return landed
    second = store.journal.begin(
        OPERATION, step="activate", fingerprint=f"landed:{PLAN_DIGEST}", actor=ACTOR
    )
    return store.journal.finish(
        OPERATION, expected=second.state, receipt="active:pi", entity_token="target-token"
    )


def _evaluate(source: AdmissionStore, target: AdmissionStore, live):
    return evaluate_chain(
        OPERATION, source_store=source, target_store=target, source_live=lambda *_a: live
    )


def test_window_1_after_hold_the_source_is_still_only_holding(tmp_path: Path) -> None:
    source, target = _stores(tmp_path)
    _hold_quiesce(source)

    resume = _evaluate(source, target, None)

    assert resume.action == "hold"
    assert resume.reason == "source-not-quiesced"
    assert resume.source_phase == "quiesce-pending"


def test_window_2_after_hold_and_finish_the_target_has_not_been_admitted(tmp_path: Path) -> None:
    source, target = _stores(tmp_path)
    _hold_quiesce(source, finish=True)

    resume = _evaluate(source, target, None)

    assert resume.action == "hold"
    assert resume.reason == "target-not-admitted"


def test_window_3_after_ship_the_bundle_is_not_a_landing(tmp_path: Path) -> None:
    source, target = _stores(tmp_path)
    _hold_quiesce(source, finish=True)
    target.prepare(OPERATION, _target_binding())

    resume = _evaluate(source, target, False)

    assert resume.action == "hold"
    assert resume.reason == "target-not-landed"


def test_window_4_after_land_the_target_is_not_yet_active(tmp_path: Path) -> None:
    source, target = _stores(tmp_path)
    _hold_quiesce(source, finish=True)
    _land(target)

    resume = _evaluate(source, target, False)

    assert resume.action == "hold"
    assert resume.reason == "target-not-active"
    assert resume.target_phase == "landed"


def test_window_4b_a_pending_landing_is_unresolved_not_landed(tmp_path: Path) -> None:
    source, target = _stores(tmp_path)
    _hold_quiesce(source, finish=True)
    _land(target, finish=False)

    resume = _evaluate(source, target, False)

    assert resume.action == "hold"
    assert resume.reason == "target-landing-unresolved"


def test_window_5_after_rebind_the_source_may_complete(tmp_path: Path) -> None:
    source, target = _stores(tmp_path)
    _hold_quiesce(source, finish=True)
    _land(target, activate=True)

    resume = _evaluate(source, target, False)

    assert resume.action == "complete"
    assert resume.reason == ""
    assert resume.may_complete is True
    assert resume.source_entity_token == "source-incarnation"


def test_window_6_before_complete_the_decision_is_idempotent(tmp_path: Path) -> None:
    source, target = _stores(tmp_path)
    _hold_quiesce(source, finish=True)
    _land(target, activate=True)

    first = _evaluate(source, target, False)
    second = _evaluate(source, target, False)

    assert first == second == replace(first, action="complete")


@pytest.mark.parametrize(
    ("live", "reason"),
    [(True, "dual-active"), (None, "liveness-unknown")],
)
def test_a_running_or_unknown_source_never_completes(tmp_path: Path, live, reason: str) -> None:
    source, target = _stores(tmp_path)
    _hold_quiesce(source, finish=True)
    _land(target, activate=True)

    resume = _evaluate(source, target, live)

    assert resume.action == "hold"
    assert resume.reason == reason


def test_a_different_plan_digest_is_not_this_chain(tmp_path: Path) -> None:
    source, target = _stores(tmp_path)
    _hold_quiesce(source, finish=True)
    target.prepare(OPERATION, replace(_target_binding(), plan_digest="b" * 64))
    start = target.journal.begin(OPERATION, step="land", fingerprint="b" * 64, actor=ACTOR)
    target.journal.finish(
        OPERATION, expected=start.state, receipt="landed:wk:3:128:1", entity_token="target-token"
    )

    resume = _evaluate(source, target, False)

    assert resume.action == "hold"
    assert resume.reason == "plan-mismatch"


def test_an_attention_row_holds_the_chain_even_with_a_proven_target(tmp_path: Path) -> None:
    source, target = _stores(tmp_path)
    _hold_quiesce(source, finish=True, attention=True)
    _land(target, activate=True)

    resume = _evaluate(source, target, False)

    assert resume.action == "hold"
    assert resume.reason == "attention:quiesce"
    assert resume.source_phase == "quiesced"


def test_the_decision_reads_both_ledgers_and_writes_neither(tmp_path: Path) -> None:
    source, target = _stores(tmp_path)
    _hold_quiesce(source, finish=True)
    _land(target, activate=True)
    before = (_rows(source._database), _rows(target._database))

    _evaluate(source, target, False)

    assert (_rows(source._database), _rows(target._database)) == before


def test_a_corrupt_target_step_row_is_raised_not_read_as_absent(tmp_path: Path) -> None:
    source, target = _stores(tmp_path)
    _hold_quiesce(source, finish=True)
    _land(target, activate=True)
    with target._database.transaction() as conn:
        conn.execute(
            "UPDATE transfer_p2_steps SET phase='bogus' WHERE operation_id=?", (OPERATION,)
        )

    with pytest.raises(AdmissionConflict) as refusal:
        _evaluate(source, target, False)

    assert refusal.value.reason.value == "corrupt-record"


def test_a_corrupt_source_step_row_is_raised_not_read_as_no_intent(tmp_path: Path) -> None:
    source, target = _stores(tmp_path)
    _hold_quiesce(source, finish=True)
    _land(target, activate=True)
    with source._database.transaction() as conn:
        conn.execute(
            "UPDATE transfer_p2_steps SET phase='bogus' WHERE operation_id=?", (OPERATION,)
        )

    with pytest.raises(AdmissionConflict) as refusal:
        _evaluate(source, target, False)

    assert refusal.value.reason.value == "corrupt-record"


def test_a_chain_this_node_does_not_speak_for_is_a_hold_not_an_error(tmp_path: Path) -> None:
    source, target = _stores(tmp_path)
    _hold_quiesce(source, finish=True)
    foreign = AdmissionStore(
        source._database,
        node="somewhere-else",
        owner=OWNER,
        clock_ms=lambda: 1000,
    )

    resume = evaluate_chain(
        OPERATION, source_store=foreign, target_store=target, source_live=lambda *_a: False
    )

    assert resume.action == "hold"
    assert resume.reason == "no-quiesce-intent"
