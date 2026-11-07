"""C status uses owner facts, not progress delivery or inbox consumption."""
from dataclasses import replace
from types import SimpleNamespace

from hyprial.daemon.impl.composition.cell_observation import CellObservationView
from hyprial.daemon.impl.harnesses.actor.contracts import HarnessProjection, _Record
from hyprial.kernel import HarnessLaunchSpec

from hyprial.daemon.impl.cells.observability.model import RunFacts
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation
from tests.daemon.cells.observability.mount.test_mount import composed as composed, settle, Process


def test_composed_projection_refreshes_owner_facts_without_hooks(composed):
    app, launcher, harness, transport, delivery, spec = composed
    settle(app, LifecycleOperation.create("create", spec))
    process = launcher.processes[-1]
    facts = RunFacts(source_epoch="turn-1", known=True, running=True, open_count=1,
                     since_ms=10, since_monotonic_ms=10)
    process.read_run_facts = lambda: facts
    harness._records["pi:worker"].process.observe_facts()
    cells = app._agent_session_domains.cells
    before = cells.run_states({}, {}, now_ms=20, monotonic_ms=20)[spec.actor]
    assert before["state"] == "in-turn", (before, harness.projection.read_run_facts(), app._agent_session_domains.agent.read_binding(spec.actor))
    facts = replace(facts, open_count=0)
    harness._records["pi:worker"].process.observe_facts()
    after = cells.run_states({}, {}, now_ms=30, monotonic_ms=30)[spec.actor]
    assert after["state"] == "idle"  # No harness reconciliation or hook publication.
    assert after["runtimeIncarnation"] == before["runtimeIncarnation"]
    assert launcher.starts == 1


def test_verified_exit_is_published_without_a_concurrent_recovery_actor():
    # A live guardian may immediately restart a dead test process. Freeze the
    # owner publication here; do not race recovery to assert its transient exit.
    entity = SimpleNamespace(uri="agent:o:n:a", actor="a", entity_token="token", block=None)
    binding = SimpleNamespace(revision=1, harness="pi", runtime="headless", session_id=None)
    agents = SimpleNamespace(read_agent=lambda _: entity, read_binding=lambda _: binding,
                             read_agents=lambda: (entity,))
    process, projection = Process(), HarnessProjection()
    record = _Record(HarnessLaunchSpec("pi", "a", True), process=process,
                     observation_entity_token="token", observation_binding_revision=1)
    cells = CellObservationView(agents, SimpleNamespace(read_session=lambda _: None), "n")
    cells.attach_harnesses(projection)
    try:
        projection.publish({"pi:a": record}, 1)
        process.running = False
        projection.publish({"pi:a": record}, 2)
        exited = cells.run_states({}, {}, now_ms=40, monotonic_ms=40)[entity.uri]
        assert exited["state"] == "not-running"
        assert not process.running
    finally:
        cells.close()
        projection.lifecycle_observations.close()


def test_agent_status_uses_shared_run_projection_and_keeps_existing_fields(composed, monkeypatch):
    app, launcher, harness, transport, delivery, spec = composed
    settle(app, LifecycleOperation.create("create", spec))
    launcher.processes[-1].read_run_facts = lambda: RunFacts(source_epoch="turn", known=True, running=True)
    harness._records["pi:worker"].process.observe_facts()
    count = 0
    read = harness.projection.read_run_facts

    def counted():
        nonlocal count
        count += 1
        return read()

    monkeypatch.setattr(harness.projection, "read_run_facts", counted)
    with app._worker_status_snapshot() as snapshot:
        row = app._agent_status_json(app.agents.get("worker"))
        again = app._agent_status_json(app.agents.get("worker"))
        assert row["runState"] == again["runState"] == snapshot.run_states[spec.actor]
        assert row["runState"]["state"] == "idle"
        assert row["uri"] == spec.actor
        assert "status" in row and "entityToken" not in row
    assert count == 1
