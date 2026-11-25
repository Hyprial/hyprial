"""Document-revision-driven member reconcile diff and digest tests (I6-2)."""

from __future__ import annotations

import uuid

from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellMember, CellScope
from hyprial.daemon.impl.cells.reconcile import (
    CELL_MEMBER_ENSURE,
    CELL_MEMBER_POLICY_APPLY,
    CELL_MEMBER_STOP,
    CellReconcileCoordinator,
    CellReconcileState,
    DictLocalMemberFacts,
    LocalMemberLaunchFacts,
    MemberEffectAction,
    MemberEffectCompletion,
    MemberEffectIntent,
    ReconcileFence,
    ReconcileMemberKey,
    ReconcileMemberPhase,
    ReconcileStepKind,
    compute_member_config_digest,
    plan_cell_reconcile,
)
from hyprial.kernel import subject_cell_id
from tests.daemon.cells.params.param_support import org_cell as param_org_cell
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
    members: tuple[tuple[str, str], ...] = (("worker", "harness"),),
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


def _facts(
    agent_id: str,
    member: str,
    *,
    harness: str = "claude",
    cwd: str = "/tmp/work",
    command: tuple[str, ...] = ("/usr/bin/claude",),
    args: tuple[str, ...] = (),
    model: str | None = None,
) -> LocalMemberLaunchFacts:
    return LocalMemberLaunchFacts(
        agent_id=agent_id,
        member=member,
        harness=harness,
        cwd=cwd,
        command=command,
        args=args,
        model=model,
    )


def test_reconcile_ignores_non_agent_cells_per_contract_oc0_v4() -> None:
    org_cell = param_org_cell(authority=OWNER_AUTHORITY)
    plan = plan_cell_reconcile(
        org_cell,
        current_members={},
        facts_port=DictLocalMemberFacts({}),
    )
    assert plan.ignored is True
    assert plan.ignore_reason == "non-agent-cell"
    assert plan.actions == ()

    effects = _RecordingEffects()
    coordinator = CellReconcileCoordinator(
        facts_port=DictLocalMemberFacts({}),
        effects_port=effects,
    )
    assert coordinator.reconcile_cell(org_cell) is None
    assert coordinator.snapshot(org_cell.cell_id) is None
    assert effects.intents == []


def test_unchanged_digest_is_noop_and_changed_digest_drains_stops_starts_and_verifies() -> None:
    aid = _agent_id()
    facts = DictLocalMemberFacts({(aid, "w1"): _facts(aid, "w1", harness="claude", model="sonnet")})
    effects = _RecordingEffects()
    coordinator = CellReconcileCoordinator(facts_port=facts, effects_port=effects)

    rev1 = _agent_cell(
        aid,
        members=(("w1", "harness"),),
        cell_members={"w1": {"kind": "claude", "model": "sonnet"}},
        revision=1,
    )
    snap1 = coordinator.reconcile_cell(rev1)
    assert snap1 is not None
    assert [i.action for i in effects.intents] == [MemberEffectAction.ENSURE]
    ensure_intent = effects.intents[0]
    assert ensure_intent.action.value == CELL_MEMBER_ENSURE
    assert ensure_intent.spec is not None
    digest_v1 = ensure_intent.spec.config_digest

    snap_ready = coordinator.complete_effect(
        MemberEffectCompletion(
            fence=ensure_intent.fence,
            attempt_id=ensure_intent.attempt_id,
            action=MemberEffectAction.ENSURE,
            succeeded=True,
            reported_digest=digest_v1,
        )
    )
    assert snap_ready is not None
    assert snap_ready.state is CellReconcileState.ACTIVE
    assert snap_ready.members[0].phase is ReconcileMemberPhase.READY
    assert coordinator.can_admit_work(rev1.cell_id, "w1") is True

    effects.intents.clear()
    rev2_same = _agent_cell(
        aid,
        members=(("w1", "harness"),),
        cell_members={"w1": {"kind": "claude", "model": "sonnet"}},
        revision=2,
        updated_at_ms=2_000,
    )
    snap2 = coordinator.reconcile_cell(rev2_same)
    assert snap2 is not None
    assert snap2.effective_revision == 2
    assert effects.intents == []
    assert snap2.members[0].phase is ReconcileMemberPhase.READY

    assert coordinator.begin_work(rev1.cell_id, "w1", "work-1") is True
    rev3_changed = _agent_cell(
        aid,
        members=(("w1", "harness"),),
        cell_members={"w1": {"kind": "claude", "model": "opus"}},
        revision=3,
    )
    snap3 = coordinator.reconcile_cell(rev3_changed)
    assert snap3 is not None
    assert coordinator.can_admit_work(rev1.cell_id, "w1") is False
    assert effects.intents == []
    assert snap3.members[0].phase is ReconcileMemberPhase.STOPPING

    assert coordinator.finish_work(rev1.cell_id, "w1", "work-1") is True
    assert len(effects.intents) == 1
    stop_intent = effects.intents[0]
    assert stop_intent.action is MemberEffectAction.STOP
    assert stop_intent.action.value == CELL_MEMBER_STOP

    snap_after_stop = coordinator.complete_effect(
        MemberEffectCompletion(
            fence=stop_intent.fence,
            attempt_id=stop_intent.attempt_id,
            action=MemberEffectAction.STOP,
            succeeded=True,
        )
    )
    assert snap_after_stop is not None
    assert len(effects.intents) == 2
    restart_intent = effects.intents[1]
    assert restart_intent.action is MemberEffectAction.ENSURE
    assert restart_intent.spec is not None
    assert restart_intent.spec.config_digest != digest_v1

    snap_restarted = coordinator.complete_effect(
        MemberEffectCompletion(
            fence=restart_intent.fence,
            attempt_id=restart_intent.attempt_id,
            action=MemberEffectAction.ENSURE,
            succeeded=True,
            reported_digest=restart_intent.spec.config_digest,
        )
    )
    assert snap_restarted is not None
    assert snap_restarted.state is CellReconcileState.ACTIVE
    assert snap_restarted.members[0].phase is ReconcileMemberPhase.READY
    assert coordinator.can_admit_work(rev1.cell_id, "w1") is True


def test_digest_mismatch_on_ready_degrades_member() -> None:
    aid = _agent_id()
    facts = DictLocalMemberFacts({(aid, "w1"): _facts(aid, "w1")})
    effects = _RecordingEffects()
    coordinator = CellReconcileCoordinator(facts_port=facts, effects_port=effects)

    rev1 = _agent_cell(
        aid,
        members=(("w1", "harness"),),
        cell_members={"w1": {"kind": "claude", "model": "sonnet"}},
        revision=1,
    )
    coordinator.reconcile_cell(rev1)
    intent = effects.intents[0]
    snap = coordinator.complete_effect(
        MemberEffectCompletion(
            fence=intent.fence,
            attempt_id=intent.attempt_id,
            action=MemberEffectAction.ENSURE,
            succeeded=True,
            reported_digest="wrong-digest",
        )
    )
    assert snap is not None
    assert snap.state is CellReconcileState.DEGRADED
    assert snap.reason == "config-digest-mismatch"
    assert snap.members[0].phase is ReconcileMemberPhase.FAILED
    assert coordinator.can_admit_work(rev1.cell_id, "w1") is False


def test_missing_reported_digest_on_ensure_degrades_member_instead_of_ready() -> None:
    aid = _agent_id()
    facts = DictLocalMemberFacts({(aid, "w1"): _facts(aid, "w1")})
    effects = _RecordingEffects()
    coordinator = CellReconcileCoordinator(facts_port=facts, effects_port=effects)

    rev1 = _agent_cell(
        aid,
        members=(("w1", "harness"),),
        cell_members={"w1": {"kind": "claude", "model": "sonnet"}},
        revision=1,
    )
    coordinator.reconcile_cell(rev1)
    intent = effects.intents[0]
    snap = coordinator.complete_effect(
        MemberEffectCompletion(
            fence=intent.fence,
            attempt_id=intent.attempt_id,
            action=MemberEffectAction.ENSURE,
            succeeded=True,
            reported_digest=None,
        )
    )
    assert snap is not None
    assert snap.state is CellReconcileState.DEGRADED
    assert snap.reason == "config-digest-missing"
    assert snap.members[0].phase is ReconcileMemberPhase.FAILED
    assert snap.members[0].running is False
    assert snap.members[0].admitting_work is False
    assert coordinator.can_admit_work(rev1.cell_id, "w1") is False


def test_removed_member_blocks_new_work_before_drain_and_rejects_stale_terminal() -> None:
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
        members=(("w1", "harness"), ("w2", "harness")),
        cell_members={"w1": {"kind": "claude"}, "w2": {"kind": "claude"}},
        entry="w1",
        revision=1,
    )
    coordinator.reconcile_cell(rev1)
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
    effects.intents.clear()
    assert coordinator.begin_work(rev1.cell_id, "w2", "work-2") is True

    rev2 = _agent_cell(
        aid,
        members=(("w1", "harness"),),
        cell_members={"w1": {"kind": "claude"}},
        entry="w1",
        revision=2,
    )
    coordinator.reconcile_cell(rev2)
    assert coordinator.can_admit_work(rev1.cell_id, "w2") is False
    assert coordinator.begin_work(rev1.cell_id, "w2", "work-new") is False
    assert coordinator.can_admit_work(rev1.cell_id, "w1") is True
    assert effects.intents == []

    assert coordinator.finish_work(rev1.cell_id, "w2", "stale-work-id") is False
    assert effects.intents == []

    assert coordinator.finish_work(rev1.cell_id, "w2", "work-2") is True
    assert len(effects.intents) == 1
    assert effects.intents[0].action is MemberEffectAction.STOP
    assert effects.intents[0].fence.member == "w2"
    snap = coordinator.complete_effect(
        MemberEffectCompletion(
            fence=effects.intents[0].fence,
            attempt_id=effects.intents[0].attempt_id,
            action=MemberEffectAction.STOP,
            succeeded=True,
        )
    )
    assert snap is not None
    assert [m.name for m in snap.members] == ["w1"]
    assert snap.state is CellReconcileState.ACTIVE
    assert CELL_MEMBER_POLICY_APPLY == "cell.member.policy.apply"
    assert ReconcileStepKind.NOOP.value == "noop"
    assert ReconcileFence(rev1.cell_id, snap.epoch, 2, "w1").member == "w1"
    assert ReconcileMemberKey(aid, "w1").member == "w1"
    assert compute_member_config_digest(
        agent_id=aid,
        member="w1",
        harness="claude",
        model=None,
        model_vendor=None,
        cwd="/tmp/work",
        command=("/usr/bin/claude",),
        args=(),
        approval=None,
        env_ref=None,
    )
