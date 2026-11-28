"""Read fenced execution facts for an active cell's registered local harness."""

from dataclasses import replace
from functools import cached_property

from hyprial.daemon.impl.cells.actor import CellActorState, CellMemberPhase
from hyprial.daemon.impl.cells.observability.model import RunFacts

from .cell_states import facts_for


class MemberObservations:
    """One request cut. Shared owner reads are taken at most once for the fleet."""

    def __init__(self, harnesses, stops):
        self.harnesses = harnesses
        self.stops = stops or {}

    @cached_property
    def managed(self):
        return self.harnesses.read_run_facts() if self.harnesses is not None else {}

    def read(self, snapshot, entity, binding):
        actor = entity.uri
        facts = facts_for(snapshot, actor)
        if snapshot is None:
            return facts
        if snapshot.source_stale:
            return replace(facts, contact_current=False)
        if snapshot.state is not CellActorState.ACTIVE:
            return facts
        member = next((m for m in snapshot.members if m.name == actor and m.role == "harness"), None)
        if member is None or member.phase is not CellMemberPhase.MISSING:
            return facts
        if binding is None:
            # A stop targets an immutable URI. A reused display name or an
            # unbound identity alone provides no evidence that a process exited.
            if entity.agent_id and actor.rsplit(":", 1)[-1] == entity.agent_id:
                receipt = self.stops.get(actor)
                if receipt:
                    return RunFacts(source_epoch=f"{snapshot.epoch}:{receipt}",
                                    known=True, exit_verified=True, process_exit_supported=True)
            return facts
        if binding.runtime != "headless" or (member.kind and member.kind != binding.harness):
            return facts
        key = (f"{binding.harness}:{entity.actor}", entity.entity_token, binding.revision)
        current = self.managed.get(key)
        if current is None:
            return facts
        return replace(current, source_epoch=f"{snapshot.epoch}:{current.source_epoch}",
                       provider_block=current.provider_block or entity.block is not None)
