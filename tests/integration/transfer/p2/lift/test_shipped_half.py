"""AT07 ③-3: the target half travels as a strict, checkable claim.

The claim is what the caller carries; the rules are still ``chain``'s.  These
tests pin both halves of that sentence: the projection refuses anything that is
not exactly the shape, and a well-formed claim can satisfy the target-side
rules but never skip one of the source's own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.transfer.p2.journal import StepState
from hyprial.daemon.impl.transfer.p2.model import Admission
from hyprial.daemon.impl.transfer.p2.orchestration.chain import (
    COMPLETE,
    HOLD,
    decide_from_rows,
)
from hyprial.daemon.impl.transfer.p2.orchestration.lift import (
    ChainPlanError,
    TargetHalf,
    admit_target,
    begin_source,
    chain_bindings,
    decide_with_shipped_target,
    half_from_json,
    half_to_json,
)
from tests.integration.transfer.p2.lift.test_chain_plan import (
    ACTOR,
    PLAN_DIGEST,
    SOURCE_NODE,
    TARGET_NODE,
    _plan,
    _store,
)

OPERATION = "chain-1"
OTHER_DIGEST = "b" * 64


def _source(tmp_path: Path, *, quiesce: bool = True, attention: bool = False):
    store = _store(tmp_path, node=SOURCE_NODE)
    begin_source(store, _plan())
    if not quiesce:
        return store
    start = store.journal.begin(
        OPERATION, step="quiesce", fingerprint=PLAN_DIGEST, actor=ACTOR
    )
    state = store.journal.finish(
        OPERATION, expected=start.state, receipt="quiesced", entity_token="source-incarnation"
    )
    if attention:
        store.journal.attention(
            OPERATION,
            expected=state,
            event_id="override-1",
            principal="operator",
            action="manual-restore",
        )
    return store


def _reported_half(tmp_path: Path, *, digest: str = PLAN_DIGEST) -> TargetHalf:
    """One landed+active target store, reported the way the target would ship it."""

    store = _store(tmp_path, node=TARGET_NODE)
    admit_target(
        store,
        chain_bindings(
            operation_id=OPERATION,
            owner="alice",
            source_node=SOURCE_NODE,
            source_actor=ACTOR,
            source_entity_token="source-incarnation",
            target_node=TARGET_NODE,
            target_actor=ACTOR,
            plan_digest=digest,
        ),
    )
    start = store.journal.begin(OPERATION, step="land", fingerprint=digest, actor=ACTOR)
    landed = store.journal.finish(
        OPERATION, expected=start.state, receipt="landed:wk:3:128:1", entity_token="target-token"
    )
    second = store.journal.begin(
        OPERATION, step="activate", fingerprint=f"landed:{digest}", actor=ACTOR
    )
    store.journal.finish(
        OPERATION, expected=second.state, receipt="active:codex", entity_token="target-token"
    )
    assert landed.phase == "landed"
    raw = half_to_json(
        TargetHalf(
            operation_id=OPERATION,
            admission=store.get(OPERATION),
            row=store.journal.get(OPERATION),
        )
    )
    return half_from_json(raw, operation_id=OPERATION)


def test_both_halves_genuine_lets_the_chain_complete(tmp_path: Path) -> None:
    half = _reported_half(tmp_path)

    decision = decide_with_shipped_target(
        OPERATION,
        source_store=_source(tmp_path),
        target_half=half,
        source_live=lambda *_a: False,
    )

    assert decision.action == COMPLETE
    assert decision.target_phase == "active"
    assert decision.source_phase == "quiesced"


@pytest.mark.parametrize("live", [None, True])
def test_a_running_or_unreadable_source_holds_the_chain(tmp_path: Path, live) -> None:
    decision = decide_with_shipped_target(
        OPERATION,
        source_store=_source(tmp_path),
        target_half=_reported_half(tmp_path),
        source_live=lambda *_a: live,
    )

    assert decision.action == HOLD
    assert decision.reason == ("liveness-unknown" if live is None else "dual-active")


def test_a_well_formed_half_cannot_unlock_a_source_with_no_quiesce_row(
    tmp_path: Path,
) -> None:
    """Negative control 1: the claim never lets the source skip its own rules."""

    decision = decide_with_shipped_target(
        OPERATION,
        source_store=_source(tmp_path, quiesce=False),
        target_half=_reported_half(tmp_path),
        source_live=lambda *_a: False,
    )

    assert decision.action == HOLD
    assert decision.reason == "no-quiesce-intent"


def test_a_well_formed_half_cannot_unlock_a_source_that_needs_attention(
    tmp_path: Path,
) -> None:
    decision = decide_with_shipped_target(
        OPERATION,
        source_store=_source(tmp_path, attention=True),
        target_half=_reported_half(tmp_path),
        source_live=lambda *_a: False,
    )

    assert decision.action == HOLD
    assert decision.reason.startswith("attention:")


def test_another_chains_half_is_refused(tmp_path: Path) -> None:
    """Negative control 2: a well-formed report for a different chain does not join."""

    decision = decide_with_shipped_target(
        OPERATION,
        source_store=_source(tmp_path),
        target_half=_reported_half(tmp_path, digest=OTHER_DIGEST),
        source_live=lambda *_a: False,
    )

    assert decision.action == HOLD
    assert decision.reason == "plan-mismatch"


def _raw_half(**changes) -> dict:
    import json

    half = {
        "version": 1,
        "operationId": OPERATION,
        "admission": {
            "operationId": OPERATION,
            "binding": {
                "role": "target",
                "owner": "alice",
                "sourceNode": SOURCE_NODE,
                "sourceAgentId": ACTOR,
                "sourceEntityToken": "source-incarnation",
                "targetNode": TARGET_NODE,
                "targetActor": ACTOR,
                "planDigest": PLAN_DIGEST,
            },
            "createdAtMs": 1,
            "expiresAtMs": 2,
            "revokedAtMs": None,
            "cancelledAtMs": None,
        },
        "row": {
            "operationId": OPERATION,
            "actor": ACTOR,
            "phase": "active",
            "step": "activate",
            "fingerprint": PLAN_DIGEST,
            "generation": 1,
            "entityToken": "target-token",
            "receipt": "active:codex",
            "needsAttention": False,
        },
    }
    for path, value in changes.items():
        node = half
        *head, last = path.split(".")
        for key in head:
            node = node[key]
        node[last] = value
    return json.loads(json.dumps(half))


def test_the_parser_refuses_anything_but_the_exact_shape() -> None:
    # it reads a good one
    assert half_from_json(_raw_half(), operation_id=OPERATION).row.phase == "active"

    cases: list[tuple[str, object]] = [
        ("version", 2),
        ("operationId", "another-op"),
        ("admission.operationId", "another-op"),
        ("admission.binding.role", "source"),
        ("admission.binding.owner", ""),
        ("row.step", "quiesce"),  # not one of the target's own steps
        ("row.actor", "someone-else"),
        ("admission.cancelledAtMs", "1"),
        ("row.operationId", "another-op"),
        ("row.generation", 0),
        ("row.needsAttention", "false"),
    ]
    for path, value in cases:
        with pytest.raises(ChainPlanError):
            half_from_json(_raw_half(**{path: value}), operation_id=OPERATION)

    missing = _raw_half()
    del missing["row"]["receipt"]
    with pytest.raises(ChainPlanError):
        half_from_json(missing, operation_id=OPERATION)

    extra = _raw_half()
    extra["row"]["targetConfirmed"] = True
    with pytest.raises(ChainPlanError):
        half_from_json(extra, operation_id=OPERATION)


def _decision_over(source_generation: int, target_generation: int, target_step: str):
    """The four rows, hand-made: the rule lives over rows, not over the store.

    The source's own counter is a single attempt per operation today
    (``journal.begin`` returns the stored step instead of opening a new
    generation), so the "older than the source's own attempt" arm of the
    staleness guard cannot be reached through the store.  It is exercised where
    it lives -- over the four rows -- so that the day a source retry path
    appears, the guard is already stated and already tested.
    """

    plan = chain_bindings(
        operation_id=OPERATION,
        owner="alice",
        source_node=SOURCE_NODE,
        source_actor=ACTOR,
        source_entity_token="source-incarnation",
        target_node=TARGET_NODE,
        target_actor=ACTOR,
        plan_digest=PLAN_DIGEST,
    )

    def admission(binding):
        return Admission(
            operation_id=OPERATION,
            binding=binding,
            created_at_ms=1,
            expires_at_ms=1 << 52,
            revoked_at_ms=None,
            cancelled_at_ms=None,
        )

    def row(step: str, generation: int):
        return StepState(
            operation_id=OPERATION,
            actor=ACTOR,
            phase="active" if step == "activate" else "landed" if step == "land" else "quiesced",
            step=step,
            fingerprint=PLAN_DIGEST,
            generation=generation,
            entity_token="incarnation",
            receipt="done",
            needs_attention=False,
        )

    return decide_from_rows(
        OPERATION,
        source_admission=admission(plan.source),
        source_row=row("quiesce", source_generation),
        target_admission=admission(plan.target),
        target_row=row(target_step, target_generation),
        source_live=lambda *_a: False,
    )


def test_a_report_older_than_the_sources_own_attempt_is_refused() -> None:
    """Condition 2(b), head arm: an old "the target is active" cannot unlock."""

    decision = _decision_over(2, 1, "land")

    assert decision.action == HOLD
    assert decision.reason == "stale-target-half"


def test_a_report_from_the_sources_own_attempt_is_let_through() -> None:
    """The guard is one-way, not a blanket refusal."""

    decision = _decision_over(2, 2, "activate")

    assert decision.action == COMPLETE


def _unfinished_half(tmp_path: Path) -> TargetHalf:
    """One target that landed and began activating, then stopped.

    Exactly the durable state a kill between the land record and the activate
    result leaves (``target.py`` writes the activate row before it calls the
    port), reported the way the target would have shipped it.
    """

    store = _store(tmp_path, node=TARGET_NODE)
    admit_target(
        store,
        chain_bindings(
            operation_id=OPERATION,
            owner="alice",
            source_node=SOURCE_NODE,
            source_actor=ACTOR,
            source_entity_token="source-incarnation",
            target_node=TARGET_NODE,
            target_actor=ACTOR,
            plan_digest=PLAN_DIGEST,
        ),
    )
    start = store.journal.begin(OPERATION, step="land", fingerprint=PLAN_DIGEST, actor=ACTOR)
    store.journal.finish(
        OPERATION, expected=start.state, receipt="landed:wk:3:128:1", entity_token="target-token"
    )
    store.journal.begin(
        OPERATION, step="activate", fingerprint=f"landed:{PLAN_DIGEST}", actor=ACTOR
    )
    raw = half_to_json(
        TargetHalf(
            operation_id=OPERATION,
            admission=store.get(OPERATION),
            row=store.journal.get(OPERATION),
        )
    )
    return half_from_json(raw, operation_id=OPERATION)


def test_an_unfinished_target_half_does_not_unlock_the_source(tmp_path: Path) -> None:
    """The crash window's other half, measured: an unresolved activation holds.

    The source's own rules are untouched and the target has not said it is
    active, so the chain holds instead of completing on a half-finished
    landing -- and the source stays quiesced rather than being released.
    """

    source = _source(tmp_path)
    decision = decide_with_shipped_target(
        OPERATION,
        source_store=source,
        target_half=_unfinished_half(tmp_path),
        source_live=lambda *_a: False,
    )

    assert decision.action == HOLD
    assert decision.reason == "target-not-active"
    assert source.journal.get(OPERATION).phase == "quiesced"
