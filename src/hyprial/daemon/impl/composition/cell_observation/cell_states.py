"""Translate cell actor snapshots into the existing run-state facts."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from hyprial.daemon.impl.cells.actor import CellMemberPhase
from hyprial.daemon.impl.cells.observability.model import RunFacts


def _member(snapshot: Any, actor: str) -> Any | None:
    members = getattr(snapshot, "members", ())
    for member in members:
        if getattr(member, "name", None) == actor:
            return member
    return None


def facts_for(snapshot: Any, actor: str) -> RunFacts:
    """Return one actor's facts from a published cell snapshot."""

    if snapshot is None:
        return RunFacts(source_epoch="cell-unavailable")
    source_epoch = str(getattr(snapshot, "epoch", "cell-unknown"))
    member = _member(snapshot, actor)
    if member is None:
        return RunFacts(source_epoch=source_epoch)
    phase = getattr(member, "phase", CellMemberPhase.MISSING)
    active = getattr(snapshot, "active_work", None)
    active_for_member = active is not None and getattr(active, "member", None) == actor
    if phase is CellMemberPhase.STARTING:
        return RunFacts(source_epoch=source_epoch, known=True, running=True,
                        operation="starting")
    if phase is CellMemberPhase.STOPPING:
        return RunFacts(source_epoch=source_epoch, known=True, running=True,
                        operation="stopping")
    if phase is CellMemberPhase.STOPPED:
        return RunFacts(source_epoch=source_epoch, known=True, exit_verified=True,
                        process_exit_supported=True)
    if phase is CellMemberPhase.FAILED:
        return RunFacts(source_epoch=source_epoch, known=False)
    if phase is CellMemberPhase.MISSING:
        return RunFacts(source_epoch=source_epoch)
    if phase is CellMemberPhase.IN_TURN or active_for_member:
        started = getattr(active, "started_at_ms", None) if active_for_member else None
        return RunFacts(source_epoch=source_epoch, known=True, running=True,
                        open_count=1, since_ms=started)
    return RunFacts(source_epoch=source_epoch, known=True, running=True)


def cell_actor_facts(agents: Any, actors: Any) -> dict[str, RunFacts]:
    """Read each cell actor independently, isolating a missing or broken cell."""

    result: dict[str, RunFacts] = {}
    for agent in agents:
        actor = str(agent.uri)
        cell_id = getattr(agent, "cell_id", None)
        try:
            snapshot = actors.snapshot(str(cell_id)) if cell_id is not None else None
        except Exception:
            snapshot = None
        facts = facts_for(snapshot, actor)
        if snapshot is not None and getattr(snapshot, "source_stale", False):
            facts = replace(facts, contact_current=False)
        result[actor] = facts
    return result


__all__ = ["cell_actor_facts", "facts_for"]
