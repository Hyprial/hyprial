"""AT07 I3-b: one chain identity, two ledgers, and the decision between them.

These tests pin the wiring, not the chain rules (those are
``tests/integration/transfer/p2/chain``): the two admissions must join, the
operation id must not drift between the two halves, and nothing here is
reachable from the P0 path yet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.state_db import StateDatabase
from hyprial.daemon.impl.transfer.p2.model import AdmissionConflict, Reason, Role
from hyprial.daemon.impl.transfer.p2.orchestration.chain import COMPLETE, HOLD
from hyprial.daemon.impl.transfer.p2.orchestration.lift import (
    ChainPlan,
    ChainPlanError,
    admit_target,
    begin_source,
    chain_bindings,
    decide,
)
from hyprial.daemon.impl.transfer.p2.store import AdmissionStore

OWNER = "alice"
SOURCE_NODE = "laptop"
TARGET_NODE = "server"
ACTOR = "wk"
PLAN_DIGEST = "a" * 64

_FACTS = dict(
    operation_id="chain-1",
    owner=OWNER,
    source_node=SOURCE_NODE,
    source_actor=ACTOR,
    source_entity_token="source-incarnation",
    target_node=TARGET_NODE,
    target_actor=ACTOR,
    plan_digest=PLAN_DIGEST,
)


def _plan(**overrides) -> ChainPlan:
    return chain_bindings(**{**_FACTS, **overrides})


def _store(tmp_path: Path, *, node: str) -> AdmissionStore:
    return AdmissionStore(
        StateDatabase(tmp_path / f"{node}-lifecycle-operations.sqlite3"),
        node=node,
        owner=OWNER,
        clock_ms=lambda: 1000,
    )


def test_the_two_sides_differ_only_by_role() -> None:
    plan = _plan()

    assert plan.source.role is Role.SOURCE
    assert plan.target.role is Role.TARGET
    # chain._same_plan compares exactly these fields; anything else drifting
    # between the two sides is a chain that holds at plan-mismatch forever.
    for name in (
        "owner",
        "source_node",
        "source_agent_id",
        "source_entity_token",
        "target_node",
        "target_actor",
        "plan_digest",
    ):
        assert getattr(plan.source, name) == getattr(plan.target, name)


def test_the_operation_id_travels_with_the_pair() -> None:
    """The two halves cannot be opened under different chain identities."""

    plan = _plan()

    assert plan.operation_id == "chain-1"


def test_one_node_id_on_both_ends_is_refused() -> None:
    with pytest.raises(ChainPlanError) as refused:
        _plan(target_node=SOURCE_NODE)

    assert "dual-active" in str(refused.value)


def test_an_invalid_digest_or_blank_field_is_refused() -> None:
    with pytest.raises(ValueError):
        _plan(plan_digest="not-a-digest")
    with pytest.raises(ValueError):
        _plan(target_actor="")


def test_each_half_is_opened_on_its_own_ledger(tmp_path: Path) -> None:
    plan = _plan()
    source_store = _store(tmp_path, node=SOURCE_NODE)
    target_store = _store(tmp_path, node=TARGET_NODE)

    source_admission = begin_source(source_store, plan)
    target_admission = admit_target(target_store, plan)

    assert source_admission.binding.role is Role.SOURCE
    assert target_admission.binding.role is Role.TARGET
    assert source_store.get(plan.operation_id) is not None
    assert target_store.get(plan.operation_id) is not None
    # The two ledgers are separate: neither store holds the other's half.
    assert target_store.get(plan.operation_id).binding.role is Role.TARGET
    assert source_store.get(plan.operation_id).binding.role is Role.SOURCE


def test_a_half_opened_on_the_wrong_node_is_refused(tmp_path: Path) -> None:
    """The ledger's own fence rejects the wrong end, before any journal row."""

    plan = _plan()
    source_store = _store(tmp_path, node=SOURCE_NODE)

    with pytest.raises(AdmissionConflict) as refused:
        admit_target(source_store, plan)

    assert refused.value.reason is Reason.BINDING_MISMATCH


def test_reopening_a_chain_returns_the_same_admission(tmp_path: Path) -> None:
    plan = _plan()
    store = _store(tmp_path, node=SOURCE_NODE)

    first = begin_source(store, plan)
    again = begin_source(store, plan)

    assert again == first


def test_a_different_plan_under_the_same_id_is_a_replay_mismatch(tmp_path: Path) -> None:
    store = _store(tmp_path, node=SOURCE_NODE)
    begin_source(store, _plan())

    with pytest.raises(AdmissionConflict) as refused:
        begin_source(store, _plan(source_entity_token="another-incarnation"))

    assert refused.value.reason is Reason.REPLAY_MISMATCH


def test_decide_reads_the_join_and_holds_an_unopened_chain(tmp_path: Path) -> None:
    """A chain with both halves open but no source intent does not complete."""

    plan = _plan()
    source_store = _store(tmp_path, node=SOURCE_NODE)
    target_store = _store(tmp_path, node=TARGET_NODE)
    begin_source(source_store, plan)
    admit_target(target_store, plan)

    resume = decide(
        plan,
        source_store=source_store,
        target_store=target_store,
        source_live=lambda actor, token: False,
    )

    assert resume.action == HOLD
    assert resume.reason == "no-quiesce-intent"
    assert resume.may_complete is False


def test_a_target_only_admission_never_completes_the_source(tmp_path: Path) -> None:
    """The negative arm: the half that was never opened cannot be assumed."""

    plan = _plan()
    source_store = _store(tmp_path, node=SOURCE_NODE)
    target_store = _store(tmp_path, node=TARGET_NODE)
    admit_target(target_store, plan)

    resume = decide(
        plan,
        source_store=source_store,
        target_store=target_store,
        source_live=lambda actor, token: False,
    )

    assert resume.action == HOLD
    assert resume.reason == "no-quiesce-intent"
    assert resume.action != COMPLETE
