"""Race and stale-cache fences do not manufacture current observations."""
from dataclasses import replace
from types import SimpleNamespace

from hyprial.daemon.impl.cells.observability.model import RunFacts
from hyprial.daemon.impl.harnesses.actor.contracts import HarnessProjection, _Record
from hyprial.daemon.impl.processes.process_owner import ProcessOwner
from hyprial.kernel import HarnessLaunchSpec
from tests.daemon.cells.observability.mount.test_mount import Process


def test_owner_generation_changed_during_read_is_conflict_not_idle():
    projection = HarnessProjection()
    process = Process()
    record = _Record(HarnessLaunchSpec("pi", "a", True), process=process,
                     observation_entity_token="token", observation_binding_revision=1)
    projection.publish({"pi:a": record}, 1)

    def read():
        projection.publish({"pi:a": replace(record, generation=record.generation + 1)}, 2)
        return RunFacts(known=True, running=True)

    process.read_run_facts = read
    try:
        facts = projection.read_run_facts()["pi:a", "token", 1]
        assert facts.conflict and not facts.known
    finally:
        projection.lifecycle_observations.close()


def test_changed_mount_cannot_publish_the_old_mounts_verified_exit():
    from hyprial.daemon.impl.cells.observability.policy import RunPolicies
    from hyprial.daemon.impl.cells.observability.projection import RunProjection
    from hyprial.daemon.impl.composition.cell_observation.run_states import run_states
    entity = SimpleNamespace(uri="agent:o:n:a", actor="a", entity_token="e", block=None)
    mounts = iter((SimpleNamespace(diagnostic_incarnation="old", lifecycle=SimpleNamespace(state="stopped", epoch="old")),
                   SimpleNamespace(diagnostic_incarnation="new", lifecycle=SimpleNamespace(state="unknown", epoch="new"))))
    view = SimpleNamespace(_harnesses=None, _coordinator=None,
                           _agents=SimpleNamespace(read_agents=lambda: (entity,)),
                           _identity=lambda _: (entity, None), _refresh=lambda _: next(mounts),
                           _runs=RunProjection(), _policies=RunPolicies.from_payload({}))
    row = run_states(view, {}, {}, now_ms=10, monotonic_ms=10)[entity.uri]
    assert row["state"] == "unknown" and row["reason"] == "conflicting-facts"


def test_existing_owner_cache_has_a_fixed_freshness_bound(monkeypatch):
    import hyprial.daemon.impl.processes.process_owner as module
    facts = RunFacts(known=True, running=True)
    owner = SimpleNamespace(facts=lambda: SimpleNamespace(run_facts=facts, run_observed_monotonic_ms=100))
    monkeypatch.setattr(module.time, "monotonic_ns", lambda: 5100 * 1_000_000)
    assert ProcessOwner.read_run_facts(owner) == facts
    monkeypatch.setattr(module.time, "monotonic_ns", lambda: 5101 * 1_000_000)
    stale = ProcessOwner.read_run_facts(owner)
    assert not stale.known and not stale.contact_current
