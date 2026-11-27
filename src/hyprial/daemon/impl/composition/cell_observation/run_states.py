"""Join owner data at one serialized cell publication boundary."""
from dataclasses import replace

from hyprial.daemon.impl.cells.observability.model import RunFacts
from .member_runtime import MemberObservations
from .mount import CellActorMount


def operation_fence(view, actor):
    """Cache-only identity/binding/process-generation cut, without mount locks."""
    entity, binding = view._identity(actor)
    if entity is None:
        return None
    key, runtime = None, None
    if binding is not None:
        key = (binding.revision, binding.harness, binding.runtime, binding.session_id)
        if binding.runtime == "headless" and view._harnesses is not None:
            runtime = view._harnesses.read_run_instance(
                f"{binding.harness}:{entity.actor}", entity.entity_token, binding.revision,
            )
        elif binding.runtime == "interactive":
            session = view._sessions.read_session(actor)
            if session is not None:
                runtime = (session.session_ref, session.process_pid, session.process_identity)
    current, after = view._identity(actor)
    if current is None or current.entity_token != entity.entity_token or after != binding:
        return None
    return entity.entity_token, key, runtime


def run_states(view, interactive, blocks, *, now_ms, monotonic_ms, cell_actors=None, completed_stops=None):
    if cell_actors is not None and not isinstance(cell_actors, CellActorMount):
        cell_actors = CellActorMount(cell_actors)
    actor_source = cell_actors is not None
    managed = (
        view._harnesses.read_run_facts()
        if not actor_source and view._harnesses is not None else {}
    )
    operations = (
        view._coordinator.read_run_operations()
        if not actor_source and view._coordinator is not None else ()
    )
    observations = MemberObservations(view._harnesses, completed_stops)
    inputs = {}
    entities = tuple(view._agents.read_agents())
    for entity in entities:
        cell = (view._refresh(entity.uri, actor_source=True)
                if actor_source else view._refresh(entity.uri))
        if cell is None:
            continue
        current, binding = view._identity(entity.uri)
        if current is None or current.entity_token != entity.entity_token:
            continue
        facts = RunFacts()
        if actor_source:
            cell_id = entity.cell_id
            snapshot = cell_actors.snapshot(cell_id) if cell_id is not None else None
            facts = observations.read(snapshot, entity, binding)
            if cell.lifecycle is not None and facts.source_epoch == "cell-unavailable":
                facts = replace(facts, source_epoch=f"{cell.lifecycle.epoch}:unavailable")
        elif binding is not None:
            if binding.runtime == "headless":
                facts = managed.get((f"{binding.harness}:{entity.actor}", entity.entity_token, binding.revision), facts)
            elif binding.runtime == "interactive":
                facts = interactive.get((entity.uri, entity.entity_token, binding.revision, binding.session_id), facts)
        # Source owners do not share a transaction. Never adopt a changed mount.
        after = (view._refresh(entity.uri, actor_source=True)
                 if actor_source else view._refresh(entity.uri))
        if after != cell or (actor_source and cell_id and cell_actors.snapshot(cell_id) != snapshot):
            inputs[entity.uri] = (cell.diagnostic_incarnation, RunFacts(conflict=True),
                                  view._policies.for_actor(entity.uri))
            continue  # The old mount's positive exit/operation is no longer current.
        if not actor_source:
            lifecycle = cell.lifecycle
            operation = lifecycle.state if lifecycle.state in {"starting", "stopping"} else None
            pending_rows = tuple(row for row in operations if row.actor == entity.uri)
            fence = operation_fence(view, entity.uri) if pending_rows else None
            for pending in pending_rows:
                if pending.fence == fence:
                    operation = pending.state
                    if operation == "stopping":
                        break
            facts = replace(
                facts,
                source_epoch=f"{lifecycle.epoch}:{facts.source_epoch}",
                exit_verified=facts.exit_verified or lifecycle.state == "stopped",
                process_exit_supported=facts.process_exit_supported or lifecycle.state == "stopped",
                operation=operation,
                provider_block=bool(blocks.get(entity.uri, entity.block is not None)),
            )
        inputs[entity.uri] = (cell.diagnostic_incarnation, facts, view._policies.for_actor(entity.uri))
    return view._runs.publish(inputs, now_ms=now_ms, monotonic_ms=monotonic_ms)
