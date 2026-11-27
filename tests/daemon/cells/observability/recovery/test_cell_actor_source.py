from dataclasses import replace
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.cells.actor import (
    CellActorMemberSnapshot,
    CellActorSnapshot,
    CellActorState,
    CellMemberPhase,
)
from hyprial.daemon.impl.cells.observability.model import RunFacts
from hyprial.identity.impl.agents.actor.ports import AgentProjection
from hyprial.kernel import subject_cell_id
from hyprial.daemon.impl.cells.observability.policy import RunPolicies
from hyprial.daemon.impl.composition.cell_observation.mount import CellActorMount
from hyprial.daemon.impl.composition.cell_observation.run_states import run_states
from hyprial.daemon.impl.composition.cell_observation import CellObservationView
from hyprial.daemon.impl.cells.observability.projection import RunProjection


ACTOR = "agent:owner:node:worker"


class Registry:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def snapshot(self, _cell_id):
        return self._snapshot


def _view(old_facts, operation, snapshot):
    entity = SimpleNamespace(uri=ACTOR, actor="worker", cell_id="cell-1",
                             entity_token="entity", block=None)
    binding = SimpleNamespace(revision=1, harness="pi", runtime="headless", session_id=None)
    cell = SimpleNamespace(diagnostic_incarnation="diagnostic", lifecycle=SimpleNamespace(
        epoch="old", state="running"))
    return SimpleNamespace(
        _agents=SimpleNamespace(read_agents=lambda: (entity,)),
        _harnesses=SimpleNamespace(read_run_facts=lambda: old_facts),
        _coordinator=SimpleNamespace(read_run_operations=lambda: operation),
        _refresh=lambda _actor, **_kwargs: cell,
        _identity=lambda _actor: (entity, binding),
        _policies=RunPolicies.from_payload({}),
        _runs=RunProjection(),
        _sessions=SimpleNamespace(read_session=lambda _actor: None),
    ), entity, CellActorMount(Registry(snapshot))


def _snapshot(phase):
    return CellActorSnapshot(
        cell_id="cell-1", epoch="cell-epoch",
        members=(CellActorMemberSnapshot(ACTOR, "harness", phase=phase),),
    )


def test_actor_ready_wins_over_legacy_running_facts():
    old = {("pi:worker", "entity", 1): SimpleNamespace(
        source_epoch="old", known=True, running=True, open_count=1)}
    view, entity, actors = _view(old, (), _snapshot(CellMemberPhase.READY))
    row = run_states(view, {}, {}, now_ms=10, monotonic_ms=10, cell_actors=actors)[entity.uri]
    assert row["state"] == "idle"


def test_actor_missing_and_recovery_ignore_legacy_operations():
    old = {("pi:worker", "entity", 1): SimpleNamespace(
        source_epoch="old", known=True, running=True)}
    view, entity, actors = _view(
        old,
        (SimpleNamespace(actor=ACTOR, fence=None, state="starting"),),
        _snapshot(CellMemberPhase.MISSING),
    )
    first = run_states(view, {}, {}, now_ms=10, monotonic_ms=10, cell_actors=actors)[entity.uri]
    assert first["state"] == "unknown"
    actors = CellActorMount(Registry(_snapshot(CellMemberPhase.READY)))
    view._coordinator.read_run_operations = lambda: (
        SimpleNamespace(actor=ACTOR, fence=None, state="stopping"),
    )
    second = run_states(view, {}, {}, now_ms=20, monotonic_ms=20, cell_actors=actors)[entity.uri]
    assert second["state"] == "idle"


def test_actor_source_refresh_does_not_read_owner_observation_sources():
    entity = SimpleNamespace(uri=ACTOR, actor="worker", cell_id="cell-1",
                             entity_token="entity")
    binding = SimpleNamespace(revision=1, harness="pi", runtime="headless", session_id=None)
    agents = SimpleNamespace(
        read_agent=lambda _actor: entity,
        read_binding=lambda _actor: binding,
    )
    harnesses = SimpleNamespace(
        read_observation_source=lambda *_args: (_ for _ in ()).throw(AssertionError("harness read")),
        lifecycle_observations=SimpleNamespace(),
    )
    sessions = SimpleNamespace(
        read_session=lambda _actor: (_ for _ in ()).throw(AssertionError("session read")),
        lifecycle_observations=SimpleNamespace(),
    )
    view = CellObservationView(agents, sessions, "node")
    view.attach_harnesses(harnesses)
    try:
        cut = view._refresh(ACTOR, actor_source=True)
        assert cut is not None
        assert cut.binding_revision == 1
    finally:
        view.close()


def test_identity_projection_provides_the_subject_cell_id():
    entity = AgentProjection(1, ACTOR, "worker", "owner", "node", "10" * 16)
    assert entity.cell_id == subject_cell_id("agent", entity.agent_id)


@pytest.mark.parametrize("facts, revision, stale, expected", [
    (RunFacts(known=True, running=True), 1, False, "idle"),
    (RunFacts(known=True, running=True, open_count=1), 1, False, "in-turn"),
    (RunFacts(known=True, running=True, open_count=1), 2, False, "unknown"),
    (RunFacts(known=True, running=True), 1, True, "unknown"),
    (RunFacts(known=True, running=True, scope="carrier-deliveries"), 1, False, "unknown"),
])
def test_managed_execution_observed_inside_active_cell(facts, revision, stale, expected):
    snap = replace(_snapshot(CellMemberPhase.MISSING), state=CellActorState.ACTIVE, source_stale=stale)
    view, entity, actors = _view({("pi:worker", "entity", revision): facts}, (), snap)
    row = run_states(view, {}, {}, now_ms=10, monotonic_ms=10, cell_actors=actors)[entity.uri]
    assert row["state"] == expected
