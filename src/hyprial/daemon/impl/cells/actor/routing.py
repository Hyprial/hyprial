"""Pure cell terminal routing over published actor snapshots (I5-4).

Decision notes (I5-4):
- Evaluates ``done``, ``next`` (using ``cells.structure.next_hop`` with I1
  fail-open fallback and completion when unmatched), and ``refused`` (no
  routing; internal ``CELL_WORK_REFUSED`` settlement reason).
- Unrecognised labels fall through ``next_hop`` to ``failOpen`` or ``done``
  without raising a routing exception; empty cells hold work ``pending``;
  retired cells settle with internal ``CELL_RETIRED``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from hyprial.daemon.impl.cells.actor.contracts import CellActorState
from hyprial.daemon.impl.cells.actor.snapshot import CellActorSnapshot
from hyprial.daemon.impl.cells.structure import (
    CellStructure,
    HopToFailOpen,
    HopToMember,
    next_hop,
)

CELL_WORK_REFUSED = "CELL_WORK_REFUSED"
CELL_RETIRED = "CELL_RETIRED"

OUTCOME_DONE = "done"
OUTCOME_NEXT = "next"
OUTCOME_REFUSED = "refused"
ROUTE_OUTCOMES = (OUTCOME_DONE, OUTCOME_NEXT, OUTCOME_REFUSED)


class CellTerminalAction(StrEnum):
    """Settled or routed outcome produced for one completed cell turn."""

    DONE = "done"
    HOP = "hop"
    PENDING = "pending"
    REFUSED = "refused"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class CellTerminalDecision:
    """Pure routing verdict of a cell member's terminal outcome."""

    action: CellTerminalAction
    member: str | None = None
    hop_index: int = 0
    reason: str | None = None
    used_fail_open: bool = False


def resolve_terminal_routing(
    snapshot: CellActorSnapshot | None,
    *,
    from_member: str,
    outcome: str,
    label: str | None = None,
    hop_index: int = 0,
) -> CellTerminalDecision:
    """Resolve the next cell action for a turn terminal outcome."""

    if not isinstance(from_member, str) or not from_member:
        raise TypeError("from_member must be a non-empty string")
    if outcome not in ROUTE_OUTCOMES:
        raise TypeError(f"unknown route outcome {outcome!r}")
    if isinstance(hop_index, bool) or not isinstance(hop_index, int) or hop_index < 0:
        raise TypeError("hop_index must be a non-negative integer")
    if outcome == OUTCOME_REFUSED:
        return CellTerminalDecision(
            action=CellTerminalAction.REFUSED,
            reason=CELL_WORK_REFUSED,
        )
    if outcome == OUTCOME_DONE:
        return CellTerminalDecision(action=CellTerminalAction.DONE)
    if not isinstance(label, str) or not label:
        raise TypeError(f"label must be a non-empty string, got {label!r}")
    if snapshot is None or snapshot.state is CellActorState.EMPTY:
        return CellTerminalDecision(action=CellTerminalAction.PENDING)
    if snapshot.state is CellActorState.RETIRED:
        return CellTerminalDecision(
            action=CellTerminalAction.RETIRED,
            reason=CELL_RETIRED,
        )
    if snapshot.state is CellActorState.DEGRADED and snapshot.effective_revision == 0:
        return CellTerminalDecision(action=CellTerminalAction.PENDING)
    structure = CellStructure(
        cell_id=snapshot.cell_id,
        members=(),
        entry=snapshot.entry_member,
        connections=snapshot.connections,
        fail_open=snapshot.fail_open,
        problems=(),
    )
    hop = next_hop(structure, from_member, label)
    if isinstance(hop, HopToMember):
        return CellTerminalDecision(
            action=CellTerminalAction.HOP,
            member=hop.member,
            hop_index=hop_index + 1,
            used_fail_open=False,
        )
    if isinstance(hop, HopToFailOpen):
        return CellTerminalDecision(
            action=CellTerminalAction.HOP,
            member=hop.member,
            hop_index=hop_index + 1,
            used_fail_open=True,
        )
    return CellTerminalDecision(action=CellTerminalAction.DONE)


__all__ = [
    "CELL_RETIRED",
    "CELL_WORK_REFUSED",
    "CellTerminalAction",
    "CellTerminalDecision",
    "OUTCOME_DONE",
    "OUTCOME_NEXT",
    "OUTCOME_REFUSED",
    "ROUTE_OUTCOMES",
    "resolve_terminal_routing",
]
