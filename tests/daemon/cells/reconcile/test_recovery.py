"""Epoch/revision/member completion fencing and actor restart recovery tests (I6-2)."""

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
    ReconcileFence,
    ReconcileMemberPhase,
)
from hyprial.kernel import create_actor_host, subject_cell_id
from tests.daemon.cells.actor.actor_support import wait_until
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
    revision: int = 1,
) -> Cell:
    return Cell(
        cell_id=subject_cell_id("agent", agent_id),
        kind="agent",
        authority=OWNER_AUTHORITY,
        scope=CellScope.of_org(),
        binding=CellBinding.subject(agent_id),
        name="worker",
        members=tuple(CellMember(ref, role) for ref, role in members),
        params={"cell.members": cell_members} if cell_members else {},
        revision=revision,
    )


def _facts(agent_id: str, member: str) -> LocalMemberLaunchFacts:
    return LocalMemberLaunchFacts(
        agent_id=agent_id,
        member=member,
        harness="claude",
        cwd="/tmp/work",
        command=("/usr/bin/claude",),
    )


def test_stale_revision_epoch_and_member_completions_never_overwrite_state() -> None:
    aid = _agent_id()
    facts = DictLocalMemberFacts({(aid, "w1"): _facts(aid, "w1")})
    effects = _RecordingEffects()
    coordinator = CellReconcileCoordinator(facts_port=facts, effects_port=effects)

    rev1 = _agent_cell(aid, cell_members={"w1": {"kind": "claude", "model": "haiku"}}, revision=1)
    coordinator.reconcile_cell(rev1)
    intent_v1 = effects.intents[0]

    rev2 = _agent_cell(aid, cell_members={"w1": {"kind": "claude", "model": "opus"}}, revision=2)
    coordinator.reconcile_cell(rev2)
    stop_v2 = effects.intents[-1]
    assert stop_v2.fence.revision == 2
    assert stop_v2.action is MemberEffectAction.STOP

    assert (
        coordinator.complete_effect(
            MemberEffectCompletion(
                fence=intent_v1.fence,
                attempt_id=intent_v1.attempt_id,
                action=MemberEffectAction.ENSURE,
                succeeded=False,
                error_code="STALE_FAILURE",
            )
        )
        is None
    )
    coordinator.complete_effect(
        MemberEffectCompletion(
            fence=stop_v2.fence,
            attempt_id=stop_v2.attempt_id,
            action=MemberEffectAction.STOP,
            succeeded=True,
        )
    )
    intent_v2 = effects.intents[-1]
    assert intent_v2.action is MemberEffectAction.ENSURE
    assert intent_v2.spec is not None

    assert (
        coordinator.complete_effect(
            MemberEffectCompletion(
                fence=ReconcileFence(rev2.cell_id, "ce-wrong-epoch", 2, "w1"),
                attempt_id=intent_v2.attempt_id,
                action=MemberEffectAction.ENSURE,
                succeeded=True,
                reported_digest=intent_v2.spec.config_digest,
            )
        )
        is None
    )
    assert (
        coordinator.complete_effect(
            MemberEffectCompletion(
                fence=ReconcileFence(rev2.cell_id, intent_v2.fence.epoch, 2, "other-member"),
                attempt_id=intent_v2.attempt_id,
                action=MemberEffectAction.ENSURE,
                succeeded=True,
                reported_digest=intent_v2.spec.config_digest,
            )
        )
        is None
    )

    snap_valid = coordinator.complete_effect(
        MemberEffectCompletion(
            fence=intent_v2.fence,
            attempt_id=intent_v2.attempt_id,
            action=MemberEffectAction.ENSURE,
            succeeded=True,
            reported_digest=intent_v2.spec.config_digest,
        )
    )
    assert snap_valid is not None
    assert snap_valid.state is CellReconcileState.ACTIVE
    assert snap_valid.members[0].phase is ReconcileMemberPhase.READY
    assert snap_valid.stale_completions_ignored == 3


def test_actor_restart_changes_epoch_and_blocks_old_epoch_completion() -> None:
    aid = _agent_id()
    facts = DictLocalMemberFacts({(aid, "w1"): _facts(aid, "w1")})
    effects = _RecordingEffects()
    host = create_actor_host()
    coordinator = CellReconcileCoordinator(
        facts_port=facts, effects_port=effects, actor_host=host
    )
    try:
        rev1 = _agent_cell(aid, cell_members={"w1": {"kind": "claude"}}, revision=1)
        coordinator.reconcile_cell(rev1)
        wait_until(
            lambda: coordinator.snapshot(rev1.cell_id) is not None
            and len(effects.intents) >= 1
        )
        snap1 = coordinator.snapshot(rev1.cell_id)
        assert snap1 is not None
        first_epoch = snap1.epoch
        first_intent = effects.intents[0]

        coordinator.restart_cell_actor(rev1.cell_id)
        wait_until(
            lambda: (snap := coordinator.snapshot(rev1.cell_id)) is not None
            and snap.epoch != first_epoch
            and len(effects.intents) >= 2
        )
        snap_restarted = coordinator.snapshot(rev1.cell_id)
        assert snap_restarted is not None

        coordinator.complete_effect(
            MemberEffectCompletion(
                fence=first_intent.fence,
                attempt_id=first_intent.attempt_id,
                action=MemberEffectAction.ENSURE,
                succeeded=True,
                reported_digest=first_intent.spec.config_digest,
            )
        )
        wait_until(
            lambda: (snap := coordinator.snapshot(rev1.cell_id)) is not None
            and snap.stale_completions_ignored >= 1
        )
        latest_intent = effects.intents[-1]
        assert latest_intent.fence.epoch == snap_restarted.epoch
        assert latest_intent.spec is not None
        coordinator.complete_effect(
            MemberEffectCompletion(
                fence=latest_intent.fence,
                attempt_id=latest_intent.attempt_id,
                action=MemberEffectAction.ENSURE,
                succeeded=True,
                reported_digest=latest_intent.spec.config_digest,
            )
        )
        wait_until(
            lambda: (snap := coordinator.snapshot(rev1.cell_id)) is not None
            and snap.state is CellReconcileState.ACTIVE
        )
    finally:
        coordinator.close()
        assert host.shutdown()


def test_stale_revision_never_overwrites_restart_recovery_source_sync_and_host() -> None:
    aid = _agent_id()
    facts = DictLocalMemberFacts({(aid, "w1"): _facts(aid, "w1")})

    for use_host in (False, True):
        effects = _RecordingEffects()
        host = create_actor_host() if use_host else None
        coordinator = CellReconcileCoordinator(
            facts_port=facts, effects_port=effects, actor_host=host
        )
        try:
            rev2 = _agent_cell(
                aid,
                cell_members={"w1": {"kind": "claude", "model": "opus"}},
                revision=2,
            )
            snap2 = coordinator.reconcile_cell(rev2)
            assert snap2 is not None
            assert snap2.revision == 2
            assert snap2.effective_revision == 2

            stale_rev1 = _agent_cell(
                aid,
                cell_members={"w1": {"kind": "claude", "model": "haiku"}},
                revision=1,
            )
            snap_after_stale = coordinator.reconcile_cell(stale_rev1)
            assert snap_after_stale is not None
            assert snap_after_stale.revision == 2
            assert snap_after_stale.effective_revision == 2

            rebuilt = coordinator.restart_cell_actor(rev2.cell_id)
            assert rebuilt is not None
            assert rebuilt.revision == 2
            assert rebuilt.effective_revision == 2
            assert effects.intents[-1].spec is not None
            assert effects.intents[-1].spec.model == "opus"
        finally:
            coordinator.close()
            if host is not None:
                assert host.shutdown()


def test_actor_host_work_admission_and_reconcile_use_real_actor_replies() -> None:
    aid = _agent_id()
    facts = DictLocalMemberFacts({(aid, "w1"): _facts(aid, "w1")})
    effects = _RecordingEffects()
    host = create_actor_host()
    coordinator = CellReconcileCoordinator(
        facts_port=facts, effects_port=effects, actor_host=host
    )
    try:
        rev1 = _agent_cell(aid, cell_members={"w1": {"kind": "claude"}}, revision=1)
        snap1 = coordinator.reconcile_cell(rev1)
        assert snap1 is not None
        assert snap1.revision == 1
        assert len(effects.intents) == 1
        intent = effects.intents[0]
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
        wait_until(lambda: coordinator.can_admit_work(rev1.cell_id, "w1"))

        assert coordinator.begin_work(rev1.cell_id, "w1", "work-1") is True
        assert coordinator.begin_work(rev1.cell_id, "w1", "work-2") is False
        assert coordinator.begin_work(rev1.cell_id, "unknown", "work-3") is False
        assert coordinator.finish_work(rev1.cell_id, "w1", "wrong-work") is False
        assert coordinator.finish_work(rev1.cell_id, "w1", "work-1") is True
        assert coordinator.begin_work(rev1.cell_id, "w1", "work-4") is True

        rev2_removed = _agent_cell(aid, members=(), revision=2)
        snap2 = coordinator.reconcile_cell(rev2_removed)
        assert snap2 is not None
        assert snap2.revision == 2
        assert coordinator.begin_work(rev1.cell_id, "w1", "work-5") is False
        assert coordinator.finish_work(rev1.cell_id, "w1", "work-4") is True

        coordinator.close()
        assert coordinator.begin_work(rev1.cell_id, "w1", "work-6") is False
        assert coordinator.finish_work(rev1.cell_id, "w1", "work-4") is False
        assert coordinator.reconcile_cell(rev2_removed) is None
    finally:
        coordinator.close()
        assert host.shutdown()


def test_same_revision_conflict_never_overwrites_restart_recovery_source_and_keeps_degraded() -> None:
    aid = _agent_id()
    facts = DictLocalMemberFacts({(aid, "w1"): _facts(aid, "w1")})

    for use_host in (False, True):
        effects = _RecordingEffects()
        host = create_actor_host() if use_host else None
        coordinator = CellReconcileCoordinator(
            facts_port=facts, effects_port=effects, actor_host=host
        )
        try:
            rev1 = _agent_cell(
                aid,
                cell_members={"w1": {"kind": "claude", "model": "sonnet"}},
                revision=1,
            )
            snap1 = coordinator.reconcile_cell(rev1)
            assert snap1 is not None
            assert len(effects.intents) == 1
            intent1 = effects.intents[0]
            assert intent1.spec is not None
            coordinator.complete_effect(
                MemberEffectCompletion(
                    fence=intent1.fence,
                    attempt_id=intent1.attempt_id,
                    action=MemberEffectAction.ENSURE,
                    succeeded=True,
                    reported_digest=intent1.spec.config_digest,
                )
            )
            wait_until(lambda: coordinator.can_admit_work(rev1.cell_id, "w1"))

            conflict_rev1 = _agent_cell(
                aid,
                cell_members={"w1": {"kind": "claude", "model": "opus"}},
                revision=1,
            )
            snap_conflict = coordinator.reconcile_cell(conflict_rev1)
            assert snap_conflict is not None
            assert snap_conflict.state is CellReconcileState.DEGRADED
            assert snap_conflict.reason == "revision-conflict"
            assert coordinator.can_admit_work(rev1.cell_id, "w1") is False

            snap_repeat_accepted = coordinator.reconcile_cell(rev1)
            assert snap_repeat_accepted is not None
            assert snap_repeat_accepted.state is CellReconcileState.DEGRADED
            assert snap_repeat_accepted.reason == "revision-conflict"

            rebuilt = coordinator.restart_cell_actor(rev1.cell_id)
            assert rebuilt is not None
            assert rebuilt.state is CellReconcileState.DEGRADED
            assert rebuilt.reason == "revision-conflict"
            assert rebuilt.revision == 1
            assert rebuilt.effective_revision == 1
            assert coordinator.can_admit_work(rev1.cell_id, "w1") is False
            assert effects.intents[-1].spec is not None
            assert effects.intents[-1].spec.model == "sonnet"

            rev2 = _agent_cell(
                aid,
                cell_members={"w1": {"kind": "claude", "model": "opus"}},
                revision=2,
            )
            snap2 = coordinator.reconcile_cell(rev2)
            assert snap2 is not None
            assert snap2.revision == 2
            assert snap2.effective_revision == 2
            assert snap2.reason != "revision-conflict"
        finally:
            coordinator.close()
            if host is not None:
                assert host.shutdown()


def test_actor_host_request_unexpected_exception_fails_closed(caplog: object) -> None:
    aid = _agent_id()
    facts = DictLocalMemberFacts({(aid, "w1"): _facts(aid, "w1")})
    effects = _RecordingEffects()
    host = create_actor_host()
    coordinator = CellReconcileCoordinator(
        facts_port=facts, effects_port=effects, actor_host=host
    )
    try:
        rev1 = _agent_cell(aid, cell_members={"w1": {"kind": "claude"}}, revision=1)
        snap1 = coordinator.reconcile_cell(rev1)
        assert snap1 is not None
        intent = effects.intents[0]
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
        wait_until(lambda: coordinator.can_admit_work(rev1.cell_id, "w1"))

        actor = coordinator._actors[rev1.cell_id]

        class _ExplodingActor:
            def tell(self, _command: object) -> object:
                raise RuntimeError("unexpected actor failure")

            def stop(self, _timeout: float) -> None:
                return None

        coordinator._actors[rev1.cell_id] = _ExplodingActor()  # type: ignore[assignment]
        try:
            rev2 = _agent_cell(
                aid,
                cell_members={"w1": {"kind": "claude", "model": "opus"}},
                revision=2,
            )
            assert coordinator.reconcile_cell(rev2) is None
            assert coordinator.begin_work(rev1.cell_id, "w1", "work-1") is False
            assert coordinator.finish_work(rev1.cell_id, "w1", "work-1") is False
            assert "unexpected actor failure" in getattr(caplog, "text", "")
        finally:
            coordinator._actors[rev1.cell_id] = actor
    finally:
        coordinator.close()
        assert host.shutdown()


def _complete(coordinator: CellReconcileCoordinator, intent: MemberEffectIntent) -> None:
    coordinator.complete_effect(
        MemberEffectCompletion(
            fence=intent.fence,
            attempt_id=intent.attempt_id,
            action=intent.action,
            succeeded=True,
            reported_digest=None if intent.spec is None or intent.action is MemberEffectAction.STOP
            else intent.spec.config_digest,
        )
    )


def test_a_restart_stop_carries_its_spec_and_the_ensure_carries_the_previous_one() -> None:
    aid = _agent_id()
    facts = DictLocalMemberFacts({(aid, "w1"): _facts(aid, "w1")})
    effects = _RecordingEffects()
    coordinator = CellReconcileCoordinator(facts_port=facts, effects_port=effects)
    coordinator.reconcile_cell(
        _agent_cell(aid, cell_members={"w1": {"kind": "claude", "model": "haiku"}}, revision=1)
    )
    first = effects.intents[-1]
    _complete(coordinator, first)

    coordinator.reconcile_cell(
        _agent_cell(aid, cell_members={"w1": {"kind": "claude", "model": "opus"}}, revision=2)
    )
    stop = effects.intents[-1]
    assert stop.action is MemberEffectAction.STOP
    assert stop.spec is not None and stop.spec.model == "opus"
    _complete(coordinator, stop)

    ensure = effects.intents[-1]
    assert ensure.action is MemberEffectAction.ENSURE
    assert ensure.previous == first.spec


def test_a_removal_during_a_restart_stop_issues_a_real_stop() -> None:
    aid = _agent_id()
    facts = DictLocalMemberFacts({(aid, "w1"): _facts(aid, "w1")})
    effects = _RecordingEffects()
    coordinator = CellReconcileCoordinator(facts_port=facts, effects_port=effects)
    coordinator.reconcile_cell(
        _agent_cell(aid, cell_members={"w1": {"kind": "claude", "model": "haiku"}}, revision=1)
    )
    _complete(coordinator, effects.intents[-1])
    rev2 = _agent_cell(aid, cell_members={"w1": {"kind": "claude", "model": "opus"}}, revision=2)
    coordinator.reconcile_cell(rev2)
    restart_stop = effects.intents[-1]
    assert restart_stop.spec is not None

    coordinator.reconcile_cell(_agent_cell(aid, members=(), revision=3))
    assert effects.intents[-1] is restart_stop
    _complete(coordinator, restart_stop)

    real_stop = effects.intents[-1]
    assert real_stop is not restart_stop
    assert real_stop.action is MemberEffectAction.STOP and real_stop.spec is None
    snapshot = coordinator.snapshot(rev2.cell_id)
    assert snapshot is not None and [member.name for member in snapshot.members] == ["w1"]
    _complete(coordinator, real_stop)
    snapshot = coordinator.snapshot(rev2.cell_id)
    assert snapshot is not None and snapshot.members == ()


def _kept(coordinator: CellReconcileCoordinator, intent: MemberEffectIntent) -> None:
    assert intent.spec is not None
    coordinator.complete_effect(
        MemberEffectCompletion(
            fence=intent.fence,
            attempt_id=intent.attempt_id,
            action=intent.action,
            succeeded=False,
            reported_digest=intent.spec.config_digest,
            error_code="POLICY_NOT_ACKNOWLEDGED",
            error_detail="harness acknowledged a different policy digest",
        )
    )


def test_a_member_kept_after_a_failed_ensure_stays_degraded_until_a_recheck_acknowledges_it(
) -> None:
    aid = _agent_id()
    facts = DictLocalMemberFacts({(aid, "w1"): _facts(aid, "w1"), (aid, "w2"): _facts(aid, "w2")})
    effects = _RecordingEffects()
    coordinator = CellReconcileCoordinator(facts_port=facts, effects_port=effects)
    rev1 = _agent_cell(aid, cell_members={"w1": {}}, revision=1)
    coordinator.reconcile_cell(rev1)
    ensure = effects.intents[-1]
    _kept(coordinator, ensure)
    snapshot = coordinator.snapshot(rev1.cell_id)
    assert snapshot is not None and snapshot.state is CellReconcileState.DEGRADED

    rev2 = _agent_cell(
        aid,
        members=(("w1", "harness"), ("w2", "harness")),
        cell_members={"w1": {}, "w2": {}},
        revision=2,
    )
    coordinator.reconcile_cell(rev2)
    assert [intent.fence.member for intent in effects.intents[1:]] == ["w2"]
    _complete(coordinator, effects.intents[-1])
    snapshot = coordinator.snapshot(rev2.cell_id)
    assert snapshot is not None and snapshot.state is CellReconcileState.DEGRADED
    w1 = next(member for member in snapshot.members if member.name == "w1")
    assert w1.admitting_work is False and w1.last_error is not None

    coordinator.reconcile_cell(rev2, recheck=("w1",))
    recheck = effects.intents[-1]
    assert recheck.fence.member == "w1" and recheck.action is MemberEffectAction.ENSURE
    _kept(coordinator, recheck)
    snapshot = coordinator.snapshot(rev2.cell_id)
    assert snapshot is not None and snapshot.state is CellReconcileState.DEGRADED

    coordinator.reconcile_cell(rev2, recheck=("w1",))
    _complete(coordinator, effects.intents[-1])
    snapshot = coordinator.snapshot(rev2.cell_id)
    assert snapshot is not None and snapshot.reason == "no-entry"
    assert all(member.admitting_work and member.last_error is None for member in snapshot.members)
