"""Pure document-revision-driven member reconcile planner (I6-2).

Decision notes (cell model slice I6-2, plan §H.2, CONTRACT-OC0-v4 §4.2 #3):

- Reconcile only plans ``kind == "agent"`` cells; any other kind is ignored
  without inspecting or mutating harness state.
- Revision conflicts (same revision with a different document digest) and any
  structure problems never diff and never half-apply member changes.
- Missing local launch facts mark only the affected member failed and hint
  ``degraded`` without disturbing healthy members or unrelated cells.
- Removed members and retiring cells always plan ``fence-drain-and-stop`` so
  new work is blocked before draining in-flight work and stopping processes.
- I6-5a: a running member whose launch configuration is unchanged but whose
  document policy changed plans ``policy-apply`` (one fenced
  ``cell.member.policy.apply`` effect: restart with the new policy, verify the
  ready acknowledgement, relaunch the last verified policy on failure).  Any
  other change, or a member with an effect still pending, keeps the
  ``drain-and-restart`` path.
- Ownership rule (I6-2 ownership fix, 2026-11-23): reconcile plans member
  effects only for harness members explicitly declared in the cell
  document's ``cell.members`` param.  A harness member without a
  declaration stays fully owned by daemon restore and the lifecycle
  commands: no facts lookup, no ensure, no stop, no restart and no
  adoption, so an agent cell without any declaration reconciles to
  ``empty``.  This preserves the behaviour the pushed I6-2 head actually
  had (its ensures for undeclared single-harness agent cells always failed
  on the agent-URI member name) and keeps restore the only restart-time
  starter of undeclared agents.  A member that loses its declaration while
  staying in the cell is not stopped and not re-planned (its last reconcile
  slot is left as it was); only a member removed from the cell, or a
  retiring cell, plans a stop.  Agent templates can seed
  ``cell.members`` through ``cellParams``; no built-in template does, so
  no release protocol is built.
- A declared member whose local launch row is held (its last known result
  is not ``running``, the same predicate daemon restore uses) is never
  started by reconcile; it plans ``fail-missing-facts`` with reason
  ``member-held`` at every revision, whoever authored it.  A stopped member
  has no launch row and already plans ``fail-missing-facts``.
- Integration critic P2-1, coordinator ruling (2026-11-24): the hold reads
  the row status alone.  No document revision releases a held row, neither
  the I6-5 registrar's daemon-authored seed nor an owner edit; only an
  explicit operator start (``hyprial agent start``, whose confirmed
  lifecycle rewrites the row to ``running`` and nudges reconcile) does.
  This replaces I6-2's "a later, higher revision may start it" and the
  revision counting it needed.  A row only turns ``failed`` after the
  harness exhausted its restart budget, so a held member that reconcile
  still records as running gets the ordinary ``fail-missing-facts`` stop of
  a process that is already down.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from hyprial.daemon.impl.cells.codec import encode
from hyprial.daemon.impl.cells.model import Cell
from hyprial.daemon.impl.cells.structure import parse_structure

from .effects import (
    DesiredMemberSpec,
    LocalMemberFactsPort,
    MemberEffectAction,
    ReconcileMemberKey,
    resolve_member_desired_spec,
)

REASON_NO_ENTRY = "no-entry"
MISSING_FACTS_PREFIX = "missing-local-facts"
REASON_REVISION_CONFLICT = "revision-conflict"
REASON_MEMBER_HELD = "member-held"


class CellReconcileState(StrEnum):
    """Cell-level reconcile states (plan §A.1, §G.2)."""

    EMPTY = "empty"
    ACTIVE = "active"
    DEGRADED = "degraded"
    RETIRING = "retiring"
    RETIRED = "retired"


class ReconcileMemberPhase(StrEnum):
    """Member-level runtime phases (plan §G.1)."""

    MISSING = "missing"
    STARTING = "starting"
    READY = "ready"
    IN_TURN = "in-turn"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class ReconcileStepKind(StrEnum):
    """Planned reconcile action per member."""

    NOOP = "noop"
    START = "start"
    DRAIN_AND_RESTART = "drain-and-restart"
    FENCE_DRAIN_AND_STOP = "fence-drain-and-stop"
    FAIL_MISSING_FACTS = "fail-missing-facts"
    POLICY_APPLY = "policy-apply"


@dataclass(frozen=True, slots=True)
class MemberRuntimeRecord:
    """Observed runtime state of one member at planning time."""

    member: str
    phase: ReconcileMemberPhase = ReconcileMemberPhase.MISSING
    active_digest: str | None = None
    running: bool = False
    admitting_work: bool = False
    in_flight_work_id: str | None = None
    last_error: str | None = None
    pending_action: MemberEffectAction | None = None
    pending_revision: int | None = None
    stop_failed: bool = False
    active_base_digest: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedMemberAction:
    """One deterministic member-level action produced by the planner."""

    key: ReconcileMemberKey
    kind: ReconcileStepKind
    desired: DesiredMemberSpec | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CellReconcilePlan:
    """Immutable diff plan for one cell revision."""

    cell_id: str
    agent_id: str
    revision: int
    document_digest: str
    ignored: bool = False
    ignore_reason: str | None = None
    rejected_bad_structure: bool = False
    problems: tuple[str, ...] = ()
    retiring: bool = False
    desired_map: Mapping[ReconcileMemberKey, DesiredMemberSpec] = MappingProxyType({})
    actions: tuple[PlannedMemberAction, ...] = ()
    target_state_hint: CellReconcileState = CellReconcileState.EMPTY
    entry_member: str | None = None


def _digest_of(cell: Cell) -> str:
    return hashlib.sha256(encode(cell).encode("utf-8")).hexdigest()


def _awaits_local_facts(current_members: Mapping[str, MemberRuntimeRecord]) -> bool:
    return any(
        record.phase is ReconcileMemberPhase.FAILED
        and (record.last_error or "").startswith(MISSING_FACTS_PREFIX)
        for record in current_members.values()
    )


def plan_cell_reconcile(
    cell: Cell,
    *,
    current_members: Mapping[str, MemberRuntimeRecord],
    facts_port: LocalMemberFactsPort,
    observed_revision: int = 0,
    observed_digest: str = "",
    document_digest: str | None = None,
    retiring: bool = False,
    replay: bool = False,
) -> CellReconcilePlan:
    """Compute the ``(agent_id, member)`` diff plan for one cell revision.

    *replay* plans a revision already applied again (members to recheck).
    """

    digest = document_digest if document_digest is not None else _digest_of(cell)
    if cell.kind != "agent":
        return CellReconcilePlan(
            cell_id=cell.cell_id,
            agent_id="",
            revision=cell.revision,
            document_digest=digest,
            ignored=True,
            ignore_reason="non-agent-cell",
        )
    agent_id = cell.binding.value if cell.binding.type == "subject" else ""
    if observed_revision > 0:
        if cell.revision < observed_revision:
            return CellReconcilePlan(
                cell_id=cell.cell_id,
                agent_id=agent_id,
                revision=cell.revision,
                document_digest=digest,
                ignored=True,
                ignore_reason="stale-revision",
            )
        if (
            cell.revision == observed_revision
            and digest == observed_digest
            and not _awaits_local_facts(current_members)
            and not replay
        ):
            return CellReconcilePlan(
                cell_id=cell.cell_id,
                agent_id=agent_id,
                revision=cell.revision,
                document_digest=digest,
                ignored=True,
                ignore_reason="duplicate-revision",
            )
        if cell.revision == observed_revision and digest != observed_digest:
            return CellReconcilePlan(
                cell_id=cell.cell_id,
                agent_id=agent_id,
                revision=cell.revision,
                document_digest=digest,
                rejected_bad_structure=True,
                problems=(REASON_REVISION_CONFLICT,),
                target_state_hint=CellReconcileState.DEGRADED,
            )

    structure = parse_structure(cell)
    if structure.problems:
        return CellReconcilePlan(
            cell_id=cell.cell_id,
            agent_id=agent_id,
            revision=cell.revision,
            document_digest=digest,
            rejected_bad_structure=True,
            problems=tuple(problem.code for problem in structure.problems),
            target_state_hint=CellReconcileState.DEGRADED,
        )

    if cell.retired or retiring:
        stop_actions = tuple(
            PlannedMemberAction(
                key=ReconcileMemberKey(agent_id or cell.cell_id, name),
                kind=ReconcileStepKind.FENCE_DRAIN_AND_STOP,
                reason="cell-retiring",
            )
            for name in sorted(current_members)
            if _needs_stop(current_members[name])
        )
        hint = (
            CellReconcileState.RETIRING
            if stop_actions or any(m.in_flight_work_id is not None for m in current_members.values())
            else CellReconcileState.RETIRED
        )
        return CellReconcilePlan(
            cell_id=cell.cell_id,
            agent_id=agent_id,
            revision=cell.revision,
            document_digest=digest,
            retiring=True,
            actions=stop_actions,
            target_state_hint=hint,
        )

    if not agent_id:
        return CellReconcilePlan(
            cell_id=cell.cell_id,
            agent_id="",
            revision=cell.revision,
            document_digest=digest,
            rejected_bad_structure=True,
            problems=("missing-agent-binding",),
            target_state_hint=CellReconcileState.DEGRADED,
        )

    desired_map: dict[ReconcileMemberKey, DesiredMemberSpec] = {}
    actions: list[PlannedMemberAction] = []
    missing_facts_problem = False
    desired_names: set[str] = set()

    declared = _declared_member_names(cell)
    managed = tuple(m for m in structure.harness_members if m.name in declared)
    present_names = {m.name for m in structure.harness_members}

    for doc_member in managed:
        desired_names.add(doc_member.name)
        key = ReconcileMemberKey(agent_id, doc_member.name)
        facts = facts_port.facts_for(agent_id, doc_member.name)
        spec, error_reason = resolve_member_desired_spec(agent_id, doc_member, facts)
        if error_reason == "external-custody":
            continue
        if spec is not None and facts is not None and facts.held:
            missing_facts_problem = True
            actions.append(
                PlannedMemberAction(
                    key=key,
                    kind=ReconcileStepKind.FAIL_MISSING_FACTS,
                    reason=REASON_MEMBER_HELD,
                )
            )
            continue
        if spec is None:
            missing_facts_problem = True
            actions.append(
                PlannedMemberAction(
                    key=key,
                    kind=ReconcileStepKind.FAIL_MISSING_FACTS,
                    reason=error_reason,
                )
            )
            continue
        desired_map[key] = spec
        current = current_members.get(doc_member.name)
        if current is None or (
            not current.running
            and not current.stop_failed
            and current.phase
            in {
                ReconcileMemberPhase.MISSING,
                ReconcileMemberPhase.STOPPED,
                ReconcileMemberPhase.FAILED,
            }
        ):
            actions.append(
                PlannedMemberAction(
                    key=key,
                    kind=ReconcileStepKind.START,
                    desired=spec,
                )
            )
        elif current.active_digest == spec.config_digest and current.phase in {
            ReconcileMemberPhase.READY,
            ReconcileMemberPhase.IN_TURN,
            ReconcileMemberPhase.STARTING,
        }:
            actions.append(
                PlannedMemberAction(
                    key=key,
                    kind=ReconcileStepKind.NOOP,
                    desired=spec,
                )
            )
        elif _policy_only_change(current, spec):
            actions.append(
                PlannedMemberAction(
                    key=key,
                    kind=ReconcileStepKind.POLICY_APPLY,
                    desired=spec,
                    reason="policy-digest-changed",
                )
            )
        else:
            actions.append(
                PlannedMemberAction(
                    key=key,
                    kind=ReconcileStepKind.DRAIN_AND_RESTART,
                    desired=spec,
                    reason=(
                        "stop-custody-pending"
                        if current.stop_failed
                        else "config-digest-changed"
                    ),
                )
            )

    for removed_name in sorted(set(current_members) - desired_names - (present_names - declared)):
        actions.append(
            PlannedMemberAction(
                key=ReconcileMemberKey(agent_id, removed_name),
                kind=ReconcileStepKind.FENCE_DRAIN_AND_STOP,
                reason="member-removed",
            )
        )

    if not managed:
        hint = CellReconcileState.EMPTY
    elif missing_facts_problem or structure.entry is None:
        hint = CellReconcileState.DEGRADED
    else:
        hint = CellReconcileState.ACTIVE

    return CellReconcilePlan(
        cell_id=cell.cell_id,
        agent_id=agent_id,
        revision=cell.revision,
        document_digest=digest,
        desired_map=MappingProxyType(desired_map),
        actions=tuple(actions),
        target_state_hint=hint,
        entry_member=structure.entry,
    )


def _policy_only_change(current: MemberRuntimeRecord, spec: DesiredMemberSpec) -> bool:
    return (
        current.running
        and not current.stop_failed
        and current.pending_action is None
        and current.active_base_digest is not None
        and current.active_base_digest == spec.base_digest
        and current.active_digest != spec.config_digest
        and current.phase in {ReconcileMemberPhase.READY, ReconcileMemberPhase.IN_TURN}
    )


def _declared_member_names(cell: Cell) -> frozenset[str]:
    raw = cell.param("cell.members")
    if not isinstance(raw, Mapping):
        return frozenset()
    return frozenset(name for name in raw if isinstance(name, str))


def _needs_stop(record: MemberRuntimeRecord) -> bool:
    return (
        record.running
        or record.stop_failed
        or record.in_flight_work_id is not None
        or record.phase
        in {
            ReconcileMemberPhase.STARTING,
            ReconcileMemberPhase.READY,
            ReconcileMemberPhase.IN_TURN,
            ReconcileMemberPhase.STOPPING,
        }
    )


__all__ = [
    "CellReconcilePlan",
    "CellReconcileState",
    "MemberRuntimeRecord",
    "PlannedMemberAction",
    "REASON_NO_ENTRY",
    "REASON_REVISION_CONFLICT",
    "ReconcileMemberPhase",
    "ReconcileStepKind",
    "plan_cell_reconcile",
]
