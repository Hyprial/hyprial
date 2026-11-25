"""Per-cell reconcile state machine: member slots, snapshots and core (I6-2).

Decision notes (I6-5a split): moved verbatim out of ``actor.py`` so the
actor host and coordinator stay below the structural line limit; the core
is the single-threaded state machine the actor handler drives.

Decision notes (I6-5a policy apply):

- ``effective_spec`` is the spec last verified running (ensure or policy
  apply); its policy digest is what the member snapshot reports.
- A ``policy-apply`` step waits for in-flight work to finish, then issues
  one ``cell.member.policy.apply`` intent carrying the new spec and the
  effective one.  Success makes the new spec effective.  A failure whose
  reported digest is the previous spec's means the effect relaunched the
  last verified policy: the member keeps running it, stops admitting work
  and the cell is degraded until a later revision applies or reverts the
  policy.  Any other failure fails the member.
- Replaying the observed revision is planned only while a member failed on
  missing local facts (a launch may have supplied them since); the replay
  acts on those members alone, so a sibling whose apply rolled back is not
  re-applied on every index generation.

Decision notes (I6-5 critic V2, P1-A/P1-B):

- A restart (drain-and-restart) STOP carries the spec it restarts into, so
  an effect port can tell it from a removal (and may defer the stop into
  the ENSURE); the following ENSURE carries the spec that was effective
  before the stop as ``previous``.  When the member is removed, retired or
  lost its facts while that STOP was in flight, its completion issues a
  real STOP instead of settling, so a deferred stop is never lost.
- An ENSURE failure that reports a digest means the effect kept or
  relaunched what ran before: the member keeps running it (effective only
  when it is ``previous``), stops admitting work and the cell is degraded,
  as for a rolled-back policy apply.  A failure without a digest fails the
  member.
- ``recheck`` names members an operator relaunched: on a revision already
  applied they are planned again when they are failed, or kept running
  after a failed ENSURE, and have no effect in flight.  A rolled-back policy
  apply is not rechecked: it stays degraded until a later revision.  Plain index generations never recheck, so a
  persistent fault does not restart a member on every generation.

Decision notes (I6-5 critic V3, P1-2): a NOOP clears a failure only when
what runs was verified (a rolled-back apply, or an ENSURE that relaunched
the verified ``previous``).  A member kept after a failed ENSURE without a
verified spec stays degraded on later revisions; a recheck issues an
ENSURE, so an operator launch is admitted only after its acknowledgement.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from hyprial.daemon.impl.cells.model import Cell

from .effects import (
    DesiredMemberSpec,
    LocalMemberFactsPort,
    MemberEffectAction,
    MemberEffectCompletion,
    MemberEffectIntent,
    MemberEffectPort,
    ReconcileFence,
)
from .planner import (
    MISSING_FACTS_PREFIX,
    REASON_NO_ENTRY,
    REASON_REVISION_CONFLICT,
    CellReconcileState,
    MemberRuntimeRecord,
    ReconcileMemberPhase,
    ReconcileStepKind,
    plan_cell_reconcile,
)


@dataclass(frozen=True, slots=True)
class _RejectedOverlay:
    observed_revision: int
    observed_digest: str
    conflict: bool
    problems: tuple[str, ...]


def _mint_epoch() -> str:
    return f"cr-{uuid.uuid4().hex}"


@dataclass(frozen=True, slots=True)
class ReconcileMemberSnapshot:
    """Published runtime snapshot for one cell member."""

    name: str
    phase: ReconcileMemberPhase
    running: bool
    admitting_work: bool
    active_digest: str | None = None
    in_flight_work_id: str | None = None
    last_error: str | None = None
    policy_digest: str | None = None


@dataclass(frozen=True, slots=True)
class CellReconcileSnapshot:
    """Published reconcile snapshot for one agent cell."""

    cell_id: str
    agent_id: str
    epoch: str
    revision: int
    effective_revision: int
    state: CellReconcileState
    reason: str | None = None
    entry_member: str | None = None
    members: tuple[ReconcileMemberSnapshot, ...] = ()
    admitting_members: frozenset[str] = frozenset()
    stale_completions_ignored: int = 0


@dataclass(slots=True)
class _MemberSlot:
    member: str
    phase: ReconcileMemberPhase = ReconcileMemberPhase.MISSING
    desired: DesiredMemberSpec | None = None
    active_digest: str | None = None
    running: bool = False
    admitting_work: bool = False
    in_flight_work_id: str | None = None
    last_error: str | None = None
    pending_intent: MemberEffectIntent | None = None
    pending_restart_spec: DesiredMemberSpec | None = None
    removing: bool = False
    removed_reason: str | None = None
    stop_failed: bool = False
    missing_facts_error: str | None = None
    effective_spec: DesiredMemberSpec | None = None
    pending_policy_spec: DesiredMemberSpec | None = None
    policy_failed: bool = False
    ensure_failed: bool = False

    def to_runtime_record(self) -> MemberRuntimeRecord:
        return MemberRuntimeRecord(
            member=self.member,
            phase=self.phase,
            active_digest=self.active_digest,
            running=self.running,
            admitting_work=self.admitting_work,
            in_flight_work_id=self.in_flight_work_id,
            last_error=self.last_error,
            pending_action=self.pending_intent.action if self.pending_intent else None,
            pending_revision=(
                self.pending_intent.fence.revision if self.pending_intent else None
            ),
            stop_failed=self.stop_failed,
            active_base_digest=(
                self.effective_spec.base_digest
                if self.effective_spec is not None and self.active_digest is not None
                else None
            ),
        )

    def to_snapshot(self) -> ReconcileMemberSnapshot:
        return ReconcileMemberSnapshot(
            name=self.member,
            phase=self.phase,
            running=self.running,
            admitting_work=self.admitting_work,
            active_digest=self.active_digest,
            in_flight_work_id=self.in_flight_work_id,
            last_error=self.last_error,
            policy_digest=(
                self.effective_spec.policy_digest
                if self.effective_spec is not None and self.active_digest is not None
                else None
            ),
        )


@dataclass(slots=True)
class _CellReconcileCore:
    cell_id: str
    agent_id: str
    epoch: str
    facts_port: LocalMemberFactsPort
    effects_port: MemberEffectPort
    observed_revision: int = 0
    observed_digest: str = ""
    effective_revision: int = 0
    effective_entry: str | None = None
    conflict: bool = False
    problems: tuple[str, ...] = ()
    retired_seen: bool = False
    last_cell: Cell | None = None
    last_rejected: _RejectedOverlay | None = None
    members: dict[str, _MemberSlot] = field(default_factory=dict)
    stale_completions_ignored: int = 0

    def apply_rejected_overlay(self, overlay: _RejectedOverlay | None) -> None:
        if overlay is None or overlay.observed_revision < self.observed_revision:
            return
        self.observed_revision = overlay.observed_revision
        self.observed_digest = overlay.observed_digest
        self.conflict = overlay.conflict
        self.problems = overlay.problems
        self.last_rejected = overlay

    def apply_document(
        self, cell: Cell, *, recheck: frozenset[str] = frozenset()
    ) -> CellReconcileSnapshot | None:
        if cell.kind != "agent":
            return None
        rechecked = self._members_to_recheck(recheck)
        current_records = {
            name: slot.to_runtime_record() for name, slot in self.members.items()
        }
        plan = plan_cell_reconcile(
            cell,
            current_members=current_records,
            facts_port=self.facts_port,
            observed_revision=self.observed_revision,
            observed_digest=self.observed_digest,
            retiring=self.retired_seen,
            replay=bool(rechecked),
        )
        if plan.ignored:
            return self.snapshot()
        if plan.rejected_bad_structure:
            if (
                REASON_REVISION_CONFLICT not in plan.problems
                and cell.revision > self.observed_revision
            ):
                self.observed_revision = cell.revision
                self.observed_digest = plan.document_digest
            self.conflict = REASON_REVISION_CONFLICT in plan.problems
            self.problems = plan.problems
            self.last_rejected = _RejectedOverlay(
                observed_revision=self.observed_revision,
                observed_digest=self.observed_digest,
                conflict=self.conflict,
                problems=self.problems,
            )
            return self.snapshot()
        awaiting = self._members_awaiting_facts() | rechecked if (
            cell.revision == self.observed_revision
            and plan.document_digest == self.observed_digest
        ) else None
        self.last_cell = cell
        self.last_rejected = None
        self.observed_revision = cell.revision
        self.observed_digest = plan.document_digest
        if plan.agent_id:
            self.agent_id = plan.agent_id

        self.conflict = False
        self.problems = ()
        self.effective_revision = cell.revision
        self.effective_entry = plan.entry_member
        if plan.retiring:
            self.retired_seen = True

        planned_names = {action.key.member for action in plan.actions}
        for action in plan.actions:
            member_name = action.key.member
            if awaiting is not None and member_name not in awaiting:
                continue
            if action.kind is ReconcileStepKind.NOOP:
                slot = self.members[member_name]
                slot.desired = action.desired
                slot.pending_policy_spec = None
                verified = slot.effective_spec is not None and action.desired is not None and (
                    slot.effective_spec.config_digest == action.desired.config_digest
                )
                if slot.ensure_failed and not verified:
                    if member_name in rechecked and action.desired is not None:
                        self._issue_ensure(slot, action.desired, cell.revision,
                                           previous=slot.effective_spec)
                    continue
                if slot.policy_failed:
                    slot.policy_failed = False
                    slot.ensure_failed = False
                    slot.last_error = None
                    slot.admitting_work = (
                        slot.phase is ReconcileMemberPhase.READY and not slot.removing
                    )
                continue
            if action.kind is ReconcileStepKind.POLICY_APPLY:
                assert action.desired is not None
                slot = self.members[member_name]
                slot.desired = action.desired
                slot.admitting_work = False
                if slot.in_flight_work_id is not None:
                    slot.pending_policy_spec = action.desired
                else:
                    self._issue_policy_apply(slot, action.desired, cell.revision)
                continue
            if action.kind is ReconcileStepKind.FAIL_MISSING_FACTS:
                slot = self.members.setdefault(member_name, _MemberSlot(member=member_name))
                reason = action.reason or "missing-local-facts"
                slot.desired = None
                slot.admitting_work = False
                slot.pending_restart_spec = None
                slot.removing = False
                slot.last_error = reason
                if (
                    slot.in_flight_work_id is not None
                    or self._member_active_or_pending(slot)
                    or slot.stop_failed
                ):
                    slot.missing_facts_error = reason
                    self._drain_or_issue_stop(slot, cell.revision, reason=reason)
                else:
                    slot.running = False
                    slot.stop_failed = False
                    slot.missing_facts_error = None
                    slot.pending_intent = None
                    slot.phase = ReconcileMemberPhase.FAILED
                continue
            if action.kind is ReconcileStepKind.START:
                assert action.desired is not None
                slot = self.members.setdefault(member_name, _MemberSlot(member=member_name))
                slot.removing = False
                slot.removed_reason = None
                slot.missing_facts_error = None
                slot.desired = action.desired
                slot.admitting_work = False
                if self._needs_stop_before_ensure(slot):
                    slot.pending_restart_spec = action.desired
                    if not slot.stop_failed:
                        slot.last_error = None
                    self._drain_or_issue_stop(
                        slot, cell.revision, reason=slot.last_error or "stop-custody-pending"
                    )
                    continue
                slot.stop_failed = False
                slot.pending_restart_spec = None
                slot.last_error = None
                self._issue_ensure(slot, action.desired, cell.revision)
                continue
            if action.kind is ReconcileStepKind.DRAIN_AND_RESTART:
                assert action.desired is not None
                slot = self.members[member_name]
                slot.admitting_work = False
                slot.removing = False
                slot.missing_facts_error = None
                slot.desired = action.desired
                slot.pending_restart_spec = action.desired
                if not slot.stop_failed:
                    slot.last_error = None
                self._drain_or_issue_stop(slot, cell.revision, reason=action.reason)
                continue
            if action.kind is ReconcileStepKind.FENCE_DRAIN_AND_STOP:
                slot = self.members.get(member_name)
                if slot is None:
                    continue
                slot.admitting_work = False
                slot.removing = True
                slot.removed_reason = action.reason
                slot.missing_facts_error = None
                slot.pending_restart_spec = None
                slot.desired = None
                if (
                    slot.in_flight_work_id is not None
                    or self._member_active_or_pending(slot)
                    or slot.stop_failed
                ):
                    self._drain_or_issue_stop(slot, cell.revision, reason=action.reason)
                else:
                    self.members.pop(member_name, None)
        if plan.retiring:
            for member_name in list(self.members):
                if member_name in planned_names:
                    continue
                slot = self.members[member_name]
                if (
                    slot.in_flight_work_id is None
                    and not self._member_active_or_pending(slot)
                    and not slot.stop_failed
                ):
                    self.members.pop(member_name, None)
        return self.snapshot()

    def _members_to_recheck(self, names: frozenset[str]) -> frozenset[str]:
        return frozenset(
            name
            for name in names
            if (slot := self.members.get(name)) is not None
            and slot.pending_intent is None
            and not slot.removing
            and (slot.phase is ReconcileMemberPhase.FAILED or slot.ensure_failed)
        )

    def _members_awaiting_facts(self) -> frozenset[str]:
        return frozenset(
            name
            for name, slot in self.members.items()
            if slot.phase is ReconcileMemberPhase.FAILED
            and (slot.last_error or "").startswith(MISSING_FACTS_PREFIX)
        )

    def can_admit_work(self, member: str) -> bool:
        if self.retired_seen or self.conflict or self.problems:
            return False
        if self._compute_state()[0] is not CellReconcileState.ACTIVE:
            return False
        slot = self.members.get(member)
        if slot is None:
            return False
        return (
            slot.admitting_work
            and not slot.removing
            and slot.phase is ReconcileMemberPhase.READY
            and slot.in_flight_work_id is None
        )

    def begin_work(self, member: str, work_id: str) -> bool:
        if not work_id or not self.can_admit_work(member):
            return False
        slot = self.members[member]
        slot.in_flight_work_id = work_id
        slot.phase = ReconcileMemberPhase.IN_TURN
        return True

    def finish_work(self, member: str, work_id: str) -> bool:
        slot = self.members.get(member)
        if slot is None or slot.in_flight_work_id != work_id:
            return False
        slot.in_flight_work_id = None
        if slot.removing:
            if self._member_active_or_pending(slot) or slot.stop_failed:
                self._issue_stop(
                    slot,
                    self.effective_revision or self.observed_revision,
                    reason=slot.removed_reason or "member-removed",
                )
            else:
                self.members.pop(member, None)
            return True
        if slot.pending_policy_spec is not None:
            self._issue_policy_apply(
                slot,
                slot.pending_policy_spec,
                self.effective_revision or self.observed_revision,
            )
            return True
        if slot.pending_restart_spec is not None:
            self._issue_stop(
                slot,
                self.effective_revision or self.observed_revision,
                reason="config-digest-changed",
            )
            return True
        if slot.missing_facts_error is not None:
            if self._member_active_or_pending(slot) or slot.stop_failed:
                self._issue_stop(
                    slot,
                    self.effective_revision or self.observed_revision,
                    reason=slot.missing_facts_error,
                )
            else:
                slot.phase = ReconcileMemberPhase.FAILED
                slot.last_error = slot.missing_facts_error
                slot.missing_facts_error = None
            return True
        if slot.phase is ReconcileMemberPhase.IN_TURN:
            slot.phase = ReconcileMemberPhase.READY
        return True

    def complete_effect(
        self, completion: MemberEffectCompletion
    ) -> CellReconcileSnapshot | None:
        fence = completion.fence
        if fence.cell_id != self.cell_id or fence.epoch != self.epoch:
            self.stale_completions_ignored += 1
            return None
        slot = self.members.get(fence.member)
        if slot is None or slot.pending_intent is None:
            self.stale_completions_ignored += 1
            return None
        pending = slot.pending_intent
        if (
            pending.fence != fence
            or pending.attempt_id != completion.attempt_id
            or pending.action is not completion.action
        ):
            self.stale_completions_ignored += 1
            return None
        slot.pending_intent = None
        if completion.action is MemberEffectAction.POLICY_APPLY:
            return self._complete_policy_apply(slot, pending, completion)
        if completion.action is MemberEffectAction.STOP:
            stopped_spec = slot.effective_spec
            slot.active_digest = None
            slot.effective_spec = None
            slot.admitting_work = False
            if not completion.succeeded:
                slot.phase = ReconcileMemberPhase.FAILED
                slot.stop_failed = True
                slot.last_error = (
                    completion.error_detail or completion.error_code or "member-stop-failed"
                )
                slot.pending_restart_spec = None
                return self.snapshot()
            slot.stop_failed = False
            if pending.spec is not None and (
                slot.removing or self.retired_seen or slot.missing_facts_error is not None
            ):
                slot.pending_restart_spec = None
                self._issue_stop(
                    slot,
                    fence.revision,
                    reason=slot.removed_reason or slot.missing_facts_error or "member-removed",
                )
                return self.snapshot()
            slot.running = False
            if slot.removing or self.retired_seen:
                self.members.pop(slot.member, None)
                return self.snapshot()
            if slot.missing_facts_error is not None:
                slot.phase = ReconcileMemberPhase.FAILED
                slot.last_error = slot.missing_facts_error
                slot.missing_facts_error = None
                slot.pending_restart_spec = None
                return self.snapshot()
            restart_spec = slot.pending_restart_spec
            slot.pending_restart_spec = None
            if restart_spec is not None:
                self._issue_ensure(slot, restart_spec, fence.revision, previous=stopped_spec)
            else:
                slot.phase = ReconcileMemberPhase.STOPPED
            return self.snapshot()

        if completion.action is MemberEffectAction.ENSURE:
            if not completion.succeeded and completion.reported_digest is not None:
                previous = pending.previous
                slot.active_digest = completion.reported_digest
                slot.effective_spec = (
                    previous
                    if previous is not None
                    and previous.config_digest == completion.reported_digest
                    else None
                )
                slot.running = True
                slot.policy_failed = True
                slot.ensure_failed = True
                slot.admitting_work = False
                slot.phase = ReconcileMemberPhase.READY
                slot.last_error = (
                    completion.error_detail or completion.error_code or "member-start-failed"
                )
                return self.snapshot()
            if not completion.succeeded:
                slot.phase = ReconcileMemberPhase.FAILED
                slot.active_digest = None
                slot.running = False
                slot.admitting_work = False
                slot.last_error = (
                    completion.error_detail or completion.error_code or "member-start-failed"
                )
                return self.snapshot()
            expected_digest = (
                slot.desired.config_digest
                if slot.desired is not None
                else (pending.spec.config_digest if pending.spec is not None else None)
            )
            if expected_digest is not None and completion.reported_digest != expected_digest:
                if completion.adopted and slot.desired is not None:
                    slot.running = True
                    slot.pending_restart_spec = slot.desired
                    self._drain_or_issue_stop(slot, fence.revision, reason="adopted-spec-drift")
                    return self.snapshot()
                slot.phase = ReconcileMemberPhase.FAILED
                slot.active_digest = None
                slot.running = False
                slot.admitting_work = False
                slot.last_error = (
                    "config-digest-missing"
                    if completion.reported_digest is None
                    else "config-digest-mismatch"
                )
                return self.snapshot()
            slot.active_digest = expected_digest
            slot.effective_spec = slot.desired or pending.spec
            slot.policy_failed = False
            slot.ensure_failed = False
            slot.running = True
            slot.stop_failed = False
            slot.missing_facts_error = None
            slot.admitting_work = not self.retired_seen and not slot.removing
            slot.phase = ReconcileMemberPhase.READY
            slot.last_error = None
            return self.snapshot()

        return self.snapshot()

    def snapshot(self) -> CellReconcileSnapshot:
        state, reason = self._compute_state()
        admitting = frozenset(
            name
            for name, slot in self.members.items()
            if state is CellReconcileState.ACTIVE
            and slot.admitting_work
            and not slot.removing
            and slot.phase is ReconcileMemberPhase.READY
        )
        entry = (
            None
            if state is not CellReconcileState.ACTIVE
            else self.effective_entry
        )
        return CellReconcileSnapshot(
            cell_id=self.cell_id,
            agent_id=self.agent_id,
            epoch=self.epoch,
            revision=self.observed_revision,
            effective_revision=self.effective_revision,
            state=state,
            reason=reason,
            entry_member=entry,
            members=tuple(
                self.members[name].to_snapshot() for name in sorted(self.members)
            ),
            admitting_members=admitting,
            stale_completions_ignored=self.stale_completions_ignored,
        )

    def _compute_state(self) -> tuple[CellReconcileState, str | None]:
        if self.retired_seen:
            failed_stop = next(
                (
                    slot
                    for slot in self.members.values()
                    if slot.phase is ReconcileMemberPhase.FAILED or slot.stop_failed
                ),
                None,
            )
            if failed_stop is not None:
                return (
                    CellReconcileState.RETIRING,
                    failed_stop.last_error or "member-stop-failed",
                )
            if any(
                slot.in_flight_work_id is not None or self._member_active_or_pending(slot)
                for slot in self.members.values()
            ):
                return CellReconcileState.RETIRING, None
            return CellReconcileState.RETIRED, None
        if self.conflict:
            return CellReconcileState.DEGRADED, REASON_REVISION_CONFLICT
        if self.problems:
            return CellReconcileState.DEGRADED, self.problems[0]
        failed = next(
            (
                slot
                for slot in self.members.values()
                if slot.phase is ReconcileMemberPhase.FAILED
                or slot.missing_facts_error is not None
                or slot.stop_failed
                or slot.policy_failed
            ),
            None,
        )
        if failed is not None:
            return (
                CellReconcileState.DEGRADED,
                failed.last_error or failed.missing_facts_error or "member-failed",
            )
        active_desired = [
            slot for slot in self.members.values() if not slot.removing and slot.desired is not None
        ]
        if not active_desired and not self.members:
            return CellReconcileState.EMPTY, None
        if not active_desired and any(slot.removing for slot in self.members.values()):
            return CellReconcileState.EMPTY, None
        if self.effective_entry is None and active_desired:
            return CellReconcileState.DEGRADED, REASON_NO_ENTRY
        return CellReconcileState.ACTIVE, None

    @staticmethod
    def _needs_stop_before_ensure(slot: _MemberSlot) -> bool:
        return (
            slot.stop_failed
            or slot.running
            or slot.in_flight_work_id is not None
            or (
                slot.pending_intent is not None
                and slot.pending_intent.action is MemberEffectAction.STOP
            )
        )

    def _drain_or_issue_stop(
        self, slot: _MemberSlot, revision: int, *, reason: str | None
    ) -> None:
        if slot.in_flight_work_id is not None or (
            slot.pending_intent is not None
            and slot.pending_intent.action is MemberEffectAction.STOP
        ):
            slot.phase = ReconcileMemberPhase.STOPPING
        else:
            self._issue_stop(slot, revision, reason=reason)

    @staticmethod
    def _member_active_or_pending(slot: _MemberSlot) -> bool:
        return (
            slot.running
            or slot.pending_intent is not None
            or slot.phase
            in {
                ReconcileMemberPhase.STARTING,
                ReconcileMemberPhase.READY,
                ReconcileMemberPhase.IN_TURN,
                ReconcileMemberPhase.STOPPING,
            }
        )

    def _issue_ensure(
        self,
        slot: _MemberSlot,
        spec: DesiredMemberSpec,
        revision: int,
        *,
        previous: DesiredMemberSpec | None = None,
    ) -> None:
        intent = MemberEffectIntent(
            fence=ReconcileFence(self.cell_id, self.epoch, revision, slot.member),
            attempt_id=uuid.uuid4().hex,
            agent_id=self.agent_id,
            action=MemberEffectAction.ENSURE,
            spec=spec,
            previous=previous,
        )
        slot.phase = ReconcileMemberPhase.STARTING
        slot.admitting_work = False
        slot.pending_intent = intent
        self.effects_port.submit(intent)

    def _issue_policy_apply(
        self, slot: _MemberSlot, spec: DesiredMemberSpec, revision: int
    ) -> None:
        intent = MemberEffectIntent(
            fence=ReconcileFence(self.cell_id, self.epoch, revision, slot.member),
            attempt_id=uuid.uuid4().hex,
            agent_id=self.agent_id,
            action=MemberEffectAction.POLICY_APPLY,
            spec=spec,
            previous=slot.effective_spec,
        )
        slot.pending_policy_spec = None
        slot.phase = ReconcileMemberPhase.STARTING
        slot.admitting_work = False
        slot.pending_intent = intent
        self.effects_port.submit(intent)

    def _complete_policy_apply(
        self,
        slot: _MemberSlot,
        pending: MemberEffectIntent,
        completion: MemberEffectCompletion,
    ) -> CellReconcileSnapshot:
        spec = pending.spec
        previous = pending.previous
        if (
            completion.succeeded
            and spec is not None
            and completion.reported_digest == spec.config_digest
        ):
            slot.active_digest = spec.config_digest
            slot.effective_spec = spec
            slot.policy_failed = False
            slot.ensure_failed = False
            slot.running = True
            slot.phase = ReconcileMemberPhase.READY
            slot.admitting_work = not self.retired_seen and not slot.removing
            slot.last_error = None
            return self.snapshot()
        error = completion.error_detail or completion.error_code or "policy-apply-failed"
        if (
            not completion.succeeded
            and previous is not None
            and completion.reported_digest == previous.config_digest
        ):
            slot.active_digest = previous.config_digest
            slot.effective_spec = previous
            slot.policy_failed = True
            slot.ensure_failed = False
            slot.running = True
            slot.phase = ReconcileMemberPhase.READY
            slot.admitting_work = False
            slot.last_error = error
            return self.snapshot()
        slot.active_digest = None
        slot.effective_spec = None
        slot.running = False
        slot.admitting_work = False
        slot.phase = ReconcileMemberPhase.FAILED
        slot.last_error = error if not completion.succeeded else "config-digest-mismatch"
        return self.snapshot()

    def _issue_stop(
        self, slot: _MemberSlot, revision: int, *, reason: str | None = None
    ) -> None:
        intent = MemberEffectIntent(
            fence=ReconcileFence(self.cell_id, self.epoch, revision, slot.member),
            attempt_id=uuid.uuid4().hex,
            agent_id=self.agent_id,
            action=MemberEffectAction.STOP,
            spec=slot.pending_restart_spec,
            reason=reason,
        )
        slot.phase = ReconcileMemberPhase.STOPPING
        slot.admitting_work = False
        slot.pending_intent = intent
        self.effects_port.submit(intent)


__all__ = [
    "CellReconcileSnapshot",
    "ReconcileMemberSnapshot",
]
