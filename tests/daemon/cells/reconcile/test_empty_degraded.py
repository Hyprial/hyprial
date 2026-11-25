"""Empty, degraded, bad-structure, isolation and retiring tests (I6-2)."""

from __future__ import annotations

import uuid

from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellMember, CellScope
from hyprial.daemon.impl.cells.reconcile import (
    CellReconcileCoordinator,
    CellReconcileState,
    DictLocalMemberFacts,
    LocalMemberLaunchFacts,
    MemberEffectAction,
    MemberEffectCompletion,
    MemberEffectIntent,
    ReconcileMemberPhase,
)
from hyprial.kernel import subject_cell_id
from tests.daemon.cells.runtime.cell_runtime_support import authority_for

OWNER_AUTHORITY = authority_for("user:owner1")


class _RecordingEffects:
    def __init__(self) -> None:
        self.intents: list[MemberEffectIntent] = []

    def submit(self, intent: MemberEffectIntent) -> None:
        self.intents.append(intent)


def _agent_id(label: str = "a") -> str:
    return f"a.{uuid.uuid4().hex[:24]}{label}"


def _agent_cell(
    agent_id: str,
    *,
    members: tuple[tuple[str, str], ...] = (("w1", "harness"),),
    cell_members: dict[str, dict[str, object]] | None = None,
    entry: str | None = None,
    connections: object | None = None,
    revision: int = 1,
    retired: bool = False,
    updated_at_ms: int = 1_000,
) -> Cell:
    params: dict[str, object] = {}
    if cell_members is not None:
        params["cell.members"] = cell_members
    if entry is not None:
        params["cell.entry"] = entry
    if connections is not None:
        params["cell.connections"] = connections
    return Cell(
        cell_id=subject_cell_id("agent", agent_id),
        kind="agent",
        authority=OWNER_AUTHORITY,
        scope=CellScope.of_org(),
        binding=CellBinding.subject(agent_id),
        name="worker",
        members=tuple(CellMember(ref, role) for ref, role in members),
        params=params,
        revision=revision,
        retired=retired,
        updated_at_ms=updated_at_ms,
    )


def _facts(agent_id: str, member: str, *, cwd: str | None = "/tmp/work") -> LocalMemberLaunchFacts:
    return LocalMemberLaunchFacts(
        agent_id=agent_id,
        member=member,
        harness="claude",
        cwd=cwd,
        command=("/usr/bin/claude",),
    )


def test_bad_structure_or_revision_conflict_does_not_diff_or_half_apply() -> None:
    aid = _agent_id()
    facts = DictLocalMemberFacts(
        {
            (aid, "w1"): _facts(aid, "w1"),
            (aid, "w2"): _facts(aid, "w2"),
        }
    )
    effects = _RecordingEffects()
    coordinator = CellReconcileCoordinator(facts_port=facts, effects_port=effects)

    rev1 = _agent_cell(
        aid,
        members=(("w1", "harness"),),
        cell_members={"w1": {"kind": "claude"}},
        revision=1,
    )
    coordinator.reconcile_cell(rev1)
    intent = effects.intents[0]
    coordinator.complete_effect(
        MemberEffectCompletion(
            fence=intent.fence,
            attempt_id=intent.attempt_id,
            action=MemberEffectAction.ENSURE,
            succeeded=True,
            reported_digest=intent.spec.config_digest,
        )
    )
    effects.intents.clear()

    bad_rev2 = _agent_cell(
        aid,
        members=(("w2", "harness"),),
        cell_members={"w2": {"kind": "claude"}},
        connections="not-a-list",
        revision=2,
    )
    snap_bad = coordinator.reconcile_cell(bad_rev2)
    assert snap_bad is not None
    assert snap_bad.state is CellReconcileState.DEGRADED
    assert snap_bad.revision == 2
    assert snap_bad.effective_revision == 1
    assert effects.intents == []
    assert [m.name for m in snap_bad.members] == ["w1"]

    conflict_rev2 = _agent_cell(
        aid,
        members=(("w2", "harness"),),
        cell_members={"w2": {"kind": "claude"}},
        revision=2,
        updated_at_ms=9_999,
    )
    snap_conflict = coordinator.reconcile_cell(conflict_rev2)
    assert snap_conflict is not None
    assert snap_conflict.state is CellReconcileState.DEGRADED
    assert snap_conflict.reason == "revision-conflict"
    assert snap_conflict.effective_revision == 1
    assert effects.intents == []


def test_missing_local_facts_and_single_member_failure_isolate_other_members_and_cells() -> None:
    aid1 = _agent_id("1")
    aid2 = _agent_id("2")
    facts = DictLocalMemberFacts(
        {
            (aid1, "good"): _facts(aid1, "good"),
            (aid1, "nocwd"): _facts(aid1, "nocwd", cwd=None),
            (aid2, "other"): _facts(aid2, "other"),
        }
    )
    effects = _RecordingEffects()
    coordinator = CellReconcileCoordinator(facts_port=facts, effects_port=effects)

    cell1 = _agent_cell(
        aid1,
        members=(("good", "harness"), ("missing", "harness"), ("nocwd", "harness")),
        cell_members={
            "good": {"kind": "claude"},
            "missing": {"kind": "claude"},
            "nocwd": {"kind": "claude"},
        },
        entry="good",
        revision=1,
    )
    cell2 = _agent_cell(
        aid2,
        members=(("other", "harness"),),
        cell_members={"other": {"kind": "claude"}},
        revision=1,
    )

    snap1 = coordinator.reconcile_cell(cell1)
    snap2 = coordinator.reconcile_cell(cell2)
    assert snap1 is not None
    assert snap2 is not None
    assert snap1.state is CellReconcileState.DEGRADED
    by_name = {m.name: m for m in snap1.members}
    assert by_name["missing"].phase is ReconcileMemberPhase.FAILED
    assert by_name["nocwd"].phase is ReconcileMemberPhase.FAILED
    assert by_name["good"].phase is ReconcileMemberPhase.STARTING

    for intent in list(effects.intents):
        assert intent.spec is not None
        coordinator.complete_effect(
            MemberEffectCompletion(
                fence=intent.fence,
                attempt_id=intent.attempt_id,
                action=MemberEffectAction.ENSURE,
                succeeded=True,
                reported_digest=intent.spec.config_digest,
            )
        )

    snap1_after = coordinator.snapshot(cell1.cell_id)
    snap2_after = coordinator.snapshot(cell2.cell_id)
    assert snap1_after is not None
    assert snap1_after.state is CellReconcileState.DEGRADED
    assert {m.name: m.phase for m in snap1_after.members}["good"] is ReconcileMemberPhase.READY
    assert snap2_after is not None
    assert snap2_after.state is CellReconcileState.ACTIVE
    assert snap2_after.members[0].phase is ReconcileMemberPhase.READY


def test_zero_members_stops_running_members_and_enters_empty_without_deleting_agent() -> None:
    aid = _agent_id()
    facts = DictLocalMemberFacts({(aid, "w1"): _facts(aid, "w1")})
    effects = _RecordingEffects()
    coordinator = CellReconcileCoordinator(facts_port=facts, effects_port=effects)

    rev1 = _agent_cell(aid, members=(("w1", "harness"),), cell_members={"w1": {"kind": "claude"}}, revision=1)
    coordinator.reconcile_cell(rev1)
    ensure = effects.intents[0]
    coordinator.complete_effect(
        MemberEffectCompletion(
            fence=ensure.fence,
            attempt_id=ensure.attempt_id,
            action=MemberEffectAction.ENSURE,
            succeeded=True,
            reported_digest=ensure.spec.config_digest,
        )
    )
    effects.intents.clear()

    rev2_zero = _agent_cell(aid, members=(), revision=2)
    snap_stopping = coordinator.reconcile_cell(rev2_zero)
    assert snap_stopping is not None
    assert coordinator.can_admit_work(rev1.cell_id, "w1") is False
    assert len(effects.intents) == 1
    assert effects.intents[0].action is MemberEffectAction.STOP

    snap_empty = coordinator.complete_effect(
        MemberEffectCompletion(
            fence=effects.intents[0].fence,
            attempt_id=effects.intents[0].attempt_id,
            action=MemberEffectAction.STOP,
            succeeded=True,
        )
    )
    assert snap_empty is not None
    assert snap_empty.state is CellReconcileState.EMPTY
    assert snap_empty.members == ()


def test_retiring_drains_in_flight_work_and_stops_members_before_retired() -> None:
    aid = _agent_id()
    facts = DictLocalMemberFacts({(aid, "w1"): _facts(aid, "w1")})
    effects = _RecordingEffects()
    coordinator = CellReconcileCoordinator(facts_port=facts, effects_port=effects)

    rev1 = _agent_cell(aid, members=(("w1", "harness"),), cell_members={"w1": {"kind": "claude"}}, revision=1)
    coordinator.reconcile_cell(rev1)
    ensure = effects.intents[0]
    coordinator.complete_effect(
        MemberEffectCompletion(
            fence=ensure.fence,
            attempt_id=ensure.attempt_id,
            action=MemberEffectAction.ENSURE,
            succeeded=True,
            reported_digest=ensure.spec.config_digest,
        )
    )
    assert coordinator.begin_work(rev1.cell_id, "w1", "work-1") is True
    effects.intents.clear()

    rev2_retired = _agent_cell(aid, members=(("w1", "harness"),), cell_members={"w1": {"kind": "claude"}}, revision=2, retired=True)
    snap_retiring = coordinator.reconcile_cell(rev2_retired)
    assert snap_retiring is not None
    assert snap_retiring.state is CellReconcileState.RETIRING
    assert coordinator.can_admit_work(rev1.cell_id, "w1") is False
    assert effects.intents == []

    assert coordinator.finish_work(rev1.cell_id, "w1", "work-1") is True
    assert len(effects.intents) == 1
    assert effects.intents[0].action is MemberEffectAction.STOP
    still_retiring = coordinator.snapshot(rev1.cell_id)
    assert still_retiring is not None and still_retiring.state is CellReconcileState.RETIRING

    snap_retired = coordinator.complete_effect(
        MemberEffectCompletion(
            fence=effects.intents[0].fence,
            attempt_id=effects.intents[0].attempt_id,
            action=MemberEffectAction.STOP,
            succeeded=True,
        )
    )
    assert snap_retired is not None
    assert snap_retired.state is CellReconcileState.RETIRED


def test_retiring_stop_failure_keeps_retiring_until_explicit_stop_succeeds() -> None:
    aid = _agent_id()
    facts = DictLocalMemberFacts({(aid, "w1"): _facts(aid, "w1")})
    effects = _RecordingEffects()
    coordinator = CellReconcileCoordinator(facts_port=facts, effects_port=effects)

    rev1 = _agent_cell(aid, members=(("w1", "harness"),), cell_members={"w1": {"kind": "claude"}}, revision=1)
    coordinator.reconcile_cell(rev1)
    ensure = effects.intents[0]
    assert ensure.spec is not None
    coordinator.complete_effect(
        MemberEffectCompletion(
            fence=ensure.fence,
            attempt_id=ensure.attempt_id,
            action=MemberEffectAction.ENSURE,
            succeeded=True,
            reported_digest=ensure.spec.config_digest,
        )
    )
    effects.intents.clear()

    rev2_retired = _agent_cell(
        aid, members=(("w1", "harness"),), cell_members={"w1": {"kind": "claude"}}, revision=2, retired=True
    )
    coordinator.reconcile_cell(rev2_retired)
    stop1 = effects.intents[0]
    assert stop1.action is MemberEffectAction.STOP

    snap_failed = coordinator.complete_effect(
        MemberEffectCompletion(
            fence=stop1.fence,
            attempt_id=stop1.attempt_id,
            action=MemberEffectAction.STOP,
            succeeded=False,
            error_code="STOP_FAILED",
            error_detail="process did not exit",
        )
    )
    assert snap_failed is not None
    assert snap_failed.state is CellReconcileState.RETIRING
    assert snap_failed.reason == "process did not exit"
    assert snap_failed.members[0].phase is ReconcileMemberPhase.FAILED
    assert coordinator.can_admit_work(rev1.cell_id, "w1") is False

    effects.intents.clear()
    rev3_retired = _agent_cell(
        aid, members=(("w1", "harness"),), cell_members={"w1": {"kind": "claude"}}, revision=3, retired=True
    )
    snap_retry = coordinator.reconcile_cell(rev3_retired)
    assert snap_retry is not None
    assert snap_retry.state is CellReconcileState.RETIRING
    assert len(effects.intents) == 1
    stop2 = effects.intents[0]
    assert stop2.action is MemberEffectAction.STOP

    snap_retired = coordinator.complete_effect(
        MemberEffectCompletion(
            fence=stop2.fence,
            attempt_id=stop2.attempt_id,
            action=MemberEffectAction.STOP,
            succeeded=True,
        )
    )
    assert snap_retired is not None
    assert snap_retired.state is CellReconcileState.RETIRED
    assert snap_retired.members == ()


def test_missing_facts_for_running_member_issues_fenced_stop_and_stays_degraded() -> None:
    aid = _agent_id()
    backing = {(aid, "w1"): _facts(aid, "w1")}

    class _MutableFacts:
        def facts_for(self, agent_id: str, member: str) -> LocalMemberLaunchFacts | None:
            return backing.get((agent_id, member))

    effects = _RecordingEffects()
    coordinator = CellReconcileCoordinator(facts_port=_MutableFacts(), effects_port=effects)

    rev1 = _agent_cell(aid, members=(("w1", "harness"),), cell_members={"w1": {"kind": "claude"}}, revision=1)
    coordinator.reconcile_cell(rev1)
    ensure = effects.intents[0]
    assert ensure.spec is not None
    coordinator.complete_effect(
        MemberEffectCompletion(
            fence=ensure.fence,
            attempt_id=ensure.attempt_id,
            action=MemberEffectAction.ENSURE,
            succeeded=True,
            reported_digest=ensure.spec.config_digest,
        )
    )
    assert coordinator.begin_work(rev1.cell_id, "w1", "work-1") is True
    effects.intents.clear()

    backing.clear()
    rev2 = _agent_cell(aid, members=(("w1", "harness"),), cell_members={"w1": {"kind": "claude"}}, revision=2)
    snap2 = coordinator.reconcile_cell(rev2)
    assert snap2 is not None
    assert snap2.state is CellReconcileState.DEGRADED
    assert snap2.reason == "missing-local-facts:w1:no-record"
    assert coordinator.can_admit_work(rev1.cell_id, "w1") is False
    assert snap2.members[0].phase is ReconcileMemberPhase.STOPPING
    assert effects.intents == []

    assert coordinator.finish_work(rev1.cell_id, "w1", "work-1") is True
    assert len(effects.intents) == 1
    stop_intent = effects.intents[0]
    assert stop_intent.action is MemberEffectAction.STOP
    assert stop_intent.fence.revision == 2

    snap_after_stop = coordinator.complete_effect(
        MemberEffectCompletion(
            fence=stop_intent.fence,
            attempt_id=stop_intent.attempt_id,
            action=MemberEffectAction.STOP,
            succeeded=True,
        )
    )
    assert snap_after_stop is not None
    assert snap_after_stop.state is CellReconcileState.DEGRADED
    assert snap_after_stop.reason == "missing-local-facts:w1:no-record"
    assert snap_after_stop.members[0].phase is ReconcileMemberPhase.FAILED
    assert snap_after_stop.members[0].running is False


def test_missing_facts_stop_failure_keeps_degraded_stopping_and_retries_fenced_stop_before_ensure() -> None:
    aid = _agent_id()
    backing = {(aid, "w1"): _facts(aid, "w1")}

    class _MutableFacts:
        def facts_for(self, agent_id: str, member: str) -> LocalMemberLaunchFacts | None:
            return backing.get((agent_id, member))

    effects = _RecordingEffects()
    coordinator = CellReconcileCoordinator(facts_port=_MutableFacts(), effects_port=effects)

    rev1 = _agent_cell(
        aid,
        members=(("w1", "harness"),),
        cell_members={"w1": {"kind": "claude", "model": "sonnet"}},
        revision=1,
    )
    coordinator.reconcile_cell(rev1)
    ensure1 = effects.intents[0]
    assert ensure1.spec is not None
    coordinator.complete_effect(
        MemberEffectCompletion(
            fence=ensure1.fence,
            attempt_id=ensure1.attempt_id,
            action=MemberEffectAction.ENSURE,
            succeeded=True,
            reported_digest=ensure1.spec.config_digest,
        )
    )
    effects.intents.clear()

    backing.clear()
    rev2 = _agent_cell(
        aid,
        members=(("w1", "harness"),),
        cell_members={"w1": {"kind": "claude", "model": "sonnet"}},
        revision=2,
    )
    snap2 = coordinator.reconcile_cell(rev2)
    assert snap2 is not None
    assert snap2.state is CellReconcileState.DEGRADED
    assert len(effects.intents) == 1
    stop2 = effects.intents[0]
    assert stop2.action is MemberEffectAction.STOP
    assert stop2.fence.revision == 2

    snap_stop_failed = coordinator.complete_effect(
        MemberEffectCompletion(
            fence=stop2.fence,
            attempt_id=stop2.attempt_id,
            action=MemberEffectAction.STOP,
            succeeded=False,
            error_code="STOP_FAILED",
            error_detail="process still alive",
        )
    )
    assert snap_stop_failed is not None
    assert snap_stop_failed.state is CellReconcileState.DEGRADED
    assert snap_stop_failed.members[0].phase is ReconcileMemberPhase.FAILED
    assert snap_stop_failed.members[0].running is True
    assert coordinator.can_admit_work(rev1.cell_id, "w1") is False

    effects.intents.clear()
    backing[(aid, "w1")] = _facts(aid, "w1")
    rev3 = _agent_cell(
        aid,
        members=(("w1", "harness"),),
        cell_members={"w1": {"kind": "claude", "model": "opus"}},
        revision=3,
    )
    snap3 = coordinator.reconcile_cell(rev3)
    assert snap3 is not None
    assert snap3.state is CellReconcileState.DEGRADED
    assert snap3.members[0].phase is ReconcileMemberPhase.STOPPING
    assert coordinator.can_admit_work(rev1.cell_id, "w1") is False
    assert [intent.action for intent in effects.intents] == [MemberEffectAction.STOP]
    stop3 = effects.intents[0]
    assert stop3.fence.revision == 3

    snap_stop3_failed = coordinator.complete_effect(
        MemberEffectCompletion(
            fence=stop3.fence,
            attempt_id=stop3.attempt_id,
            action=MemberEffectAction.STOP,
            succeeded=False,
            error_code="STOP_FAILED",
            error_detail="still stuck",
        )
    )
    assert snap_stop3_failed is not None
    assert snap_stop3_failed.state is CellReconcileState.DEGRADED
    assert snap_stop3_failed.members[0].phase is ReconcileMemberPhase.FAILED
    assert snap_stop3_failed.members[0].running is True

    effects.intents.clear()
    rev4 = _agent_cell(
        aid,
        members=(("w1", "harness"),),
        cell_members={"w1": {"kind": "claude", "model": "opus"}},
        revision=4,
    )
    snap4 = coordinator.reconcile_cell(rev4)
    assert snap4 is not None
    assert snap4.state is CellReconcileState.DEGRADED
    assert snap4.members[0].phase is ReconcileMemberPhase.STOPPING
    assert [intent.action for intent in effects.intents] == [MemberEffectAction.STOP]
    stop4 = effects.intents[0]
    assert stop4.fence.revision == 4

    snap_stop4_ok = coordinator.complete_effect(
        MemberEffectCompletion(
            fence=stop4.fence,
            attempt_id=stop4.attempt_id,
            action=MemberEffectAction.STOP,
            succeeded=True,
        )
    )
    assert snap_stop4_ok is not None
    assert len(effects.intents) == 2
    ensure4 = effects.intents[1]
    assert ensure4.action is MemberEffectAction.ENSURE
    assert ensure4.fence.revision == 4
    assert ensure4.spec is not None
    assert ensure4.spec.model == "opus"

    snap_active = coordinator.complete_effect(
        MemberEffectCompletion(
            fence=ensure4.fence,
            attempt_id=ensure4.attempt_id,
            action=MemberEffectAction.ENSURE,
            succeeded=True,
            reported_digest=ensure4.spec.config_digest,
        )
    )
    assert snap_active is not None
    assert snap_active.state is CellReconcileState.ACTIVE
    assert snap_active.members[0].phase is ReconcileMemberPhase.READY
    assert coordinator.can_admit_work(rev1.cell_id, "w1") is True
