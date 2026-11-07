"""Cell attachment composes existing owners without acquiring business authority."""

import dataclasses
from types import SimpleNamespace

import pytest

from hyprial.daemon import cell_observation
from hyprial.daemon.impl.composition import AgentIdentityProjectionView, CorrelatedDomainEvents, HarnessPortClient
from hyprial.daemon.impl.composition.cell_observation import CellObservationView
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.actor.contracts import HarnessProjection, _Record
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessLaunchProjection
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation, LifecycleSpec, LifecycleState
from hyprial.daemon.impl.network.route_ports import RouteSpec
from hyprial.kernel import HarnessLaunchSpec, PortAdmission, ProcessLiveness, ProcessLivenessState
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import (
    _isolated_application, _close_application, _Transport, _LocalDelivery, _Inbox,
    TERMINAL_WAIT_SECONDS,
)


class Process:
    running = True
    pid = None

    def stop(self):
        self.running = False

    def liveness(self):
        return ProcessLiveness(ProcessLivenessState.ALIVE if self.running else ProcessLivenessState.DEAD,
                               observed=True)


class Launcher:
    def __init__(self):
        self.starts = 0
        self.processes = []

    def start(self, _spec):
        self.starts += 1
        process = Process()
        self.processes.append(process)
        return process


@pytest.fixture
def composed(tmp_path):
    app = _isolated_application(state_dir=tmp_path / "state", socket_path=tmp_path / "daemon.sock",
                                node_id="node", owner="owner", network_isolated=True)
    events = CorrelatedDomainEvents()
    launcher = Launcher()
    harness = HarnessRuntimeActor(launcher, event_sink=events, desired_state=app.desired_state,
                                 agent_identity=AgentIdentityProjectionView(app._agent_session_domains))
    harnesses = HarnessPortClient(harness, events)
    app._harnesses = harnesses
    transport, delivery = _Transport(), _LocalDelivery()
    app._start_lifecycle_manager(transport=transport, inbox=_Inbox(), local_delivery=delivery,
                                 harnesses=harnesses)
    spec = LifecycleSpec("worker", "agent:owner:node:worker", HarnessLaunchProjection("pi", "worker", True),
                         RouteSpec("agent:owner:node:worker", "live/worker", "inbox/worker"))
    try:
        yield app, launcher, harness, transport, delivery, spec
    finally:
        _close_application(app)


def settle(app, operation):
    manager = app._lifecycle_manager
    assert manager.submit(operation) is PortAdmission.ACCEPTED
    assert manager.wait(operation.operation_id, TERMINAL_WAIT_SECONDS).state is LifecycleState.COMPLETED


def test_mount_survives_existing_lifecycle_and_has_no_identity_or_route_effect(composed):
    app, launcher, harness, transport, delivery, spec = composed
    settle(app, LifecycleOperation.create("create", spec))
    domains = app._agent_session_domains
    before = domains.agent.read_agent("worker")
    binding = domains.agent.read_binding(spec.actor)
    routes = app._routes.owners(spec.actor)
    registrations = (len(transport.registrations), len(delivery.registrations))
    cells = domains.cells
    assert isinstance(cells, cell_observation.CellObservationPort)
    first = cells.snapshot(spec.actor)
    assert isinstance(first, cell_observation.CellSnapshot)
    assert first.actor == before.uri == spec.actor
    assert first.custody == "managed"
    assert first.lifecycle.state == "running"
    assert first.binding_revision == binding.revision
    assert before.entity_token not in repr(dataclasses.asdict(first))
    assert first.target.group == "harness.lifecycle"
    assert first.target.key.startswith(spec.actor + "/")
    assert domains.agents.cell("worker") == first
    for _ in range(4):
        assert cells.snapshot(spec.actor) == first
    assert domains.agent.read_agent("worker") == before
    assert domains.agent.read_binding(spec.actor) == binding
    assert app._routes.owners(spec.actor) == routes
    assert (len(transport.registrations), len(delivery.registrations)) == registrations
    assert launcher.starts == 1
    settle(app, LifecycleOperation.deactivate("stop", spec))
    stopped = cells.snapshot(spec.actor)
    assert stopped.diagnostic_incarnation == first.diagnostic_incarnation
    assert stopped.actor == first.actor
    settle(app, LifecycleOperation.resume("restart", spec))
    restarted = cells.snapshot(spec.actor)
    assert restarted.diagnostic_incarnation == first.diagnostic_incarnation
    assert restarted.lifecycle.state == "running"
    assert restarted.lifecycle.epoch != first.lifecycle.epoch
    assert launcher.starts == 2


def test_old_agent_and_source_cannot_update_recreated_cell(composed):
    app, launcher, harness, transport, delivery, spec = composed
    settle(app, LifecycleOperation.create("create", spec))
    cells = app._agent_session_domains.cells
    old = cells.snapshot(spec.actor)
    old_entity = app._agent_session_domains.agent.read_agent("worker").entity_token
    settle(app, LifecycleOperation.remove("remove", spec))
    assert cells.snapshot(spec.actor) is None
    settle(app, LifecycleOperation.create("create-again", spec))
    new = cells.snapshot(spec.actor)
    assert new.actor == old.actor and new.diagnostic_incarnation != old.diagnostic_incarnation
    assert cells.observe(spec.actor, epoch=old.lifecycle.epoch,
                         after_revision=old.lifecycle.revision).resync_required
    source = harness.projection.read_observation_source("pi:worker", old_entity, new.binding_revision)
    assert source is None
    assert cells.snapshot(spec.actor) == new


def test_registration_does_not_claim_runtime_and_read_only_port_has_no_controls(composed):
    app, launcher, *_rest = composed
    app.agents.create("unstarted")
    cell = app._agent_session_domains.cells.snapshot("agent:owner:node:unstarted")
    assert cell.lifecycle.state == "unknown" and not cell.lifecycle.available
    assert cell.custody == "none"
    assert launcher.starts == 0
    for name in ("start", "stop", "send", "receive", "reply", "ack", "approve"):
        assert not hasattr(app._agent_session_domains.cells, name)


def test_mount_rollback_restart_and_external_custody(composed, monkeypatch):
    app, launcher, harness, transport, delivery, spec = composed
    settle(app, LifecycleOperation.create("create", spec))
    domains = app._agent_session_domains
    cells = domains.cells
    first = cells.snapshot(spec.actor)
    old_reader = harness.projection.lifecycle_observations.reader
    opened = old_reader.open(first.target)
    routes = app._routes.owners(spec.actor)
    starts = launcher.starts
    cells.close()
    assert old_reader.observe(first.target, observer_id=opened.observer_id).status == "closed"
    assert launcher.processes[-1].running
    fresh = CellObservationView(domains.agent, domains.session, "node")
    fresh.attach_harnesses(harness.projection)
    owner = harness.projection.lifecycle_observations
    bind = owner.bind

    def fail_after_bind(*args):
        bind(*args)
        raise RuntimeError("injected mid-mount failure")

    with monkeypatch.context() as patch:
        patch.setattr(owner, "bind", fail_after_bind)
        with pytest.raises(RuntimeError, match="mid-mount"):
            fresh.snapshot(spec.actor)
        assert not owner._bindings
    recovered = fresh.snapshot(spec.actor)
    assert recovered.lifecycle.state == "running"
    assert app._routes.owners(spec.actor) == routes
    assert launcher.starts == starts
    fresh.close()


def test_bound_runtime_death_keeps_cell_and_never_executes_policy():
    entity = SimpleNamespace(uri="agent:o:host:a", actor="a", entity_token="private-token")
    binding = SimpleNamespace(revision=1, harness="pi", runtime="headless", session_id=None)
    agents = SimpleNamespace(read_agent=lambda _actor: entity, read_binding=lambda _actor: binding,
                             read_agents=lambda: (entity,))
    session = SimpleNamespace(read_session=lambda _actor: None)
    projection = HarnessProjection()
    process = Process()
    record = _Record(HarnessLaunchSpec("pi", "a", True), process=process,
                     observation_entity_token=entity.entity_token, observation_binding_revision=1)
    projection.publish({"pi:a": record}, 1)
    cells = CellObservationView(agents, session, "host")
    cells.attach_harnesses(projection)
    try:
        before = cells.snapshot(entity.uri)
        process.running = False
        projection.publish({"pi:a": record}, 2)
        after = cells.snapshot(entity.uri)
        assert after.diagnostic_incarnation == before.diagnostic_incarnation
        assert after.lifecycle.state == "stopped"
        assert cells.observe(entity.uri, epoch=before.lifecycle.epoch,
                             after_revision=before.lifecycle.revision).events
        assert not process.running
    finally:
        cells.close()
        projection.lifecycle_observations.close()
