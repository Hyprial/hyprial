"""Additional B acceptance: external custody, binding fences and bounded mounts."""

import dataclasses
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.composition.cell_observation import CellObservationView
from hyprial.daemon.impl.operations.session_ports import RegisterSessionCommand, SessionMutationCompleted
from hyprial.kernel import CHANNEL_PROTOCOL_VERSION
from tests.daemon.cells.observability.mount.test_mount import composed as composed, settle, Process
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation
from hyprial.daemon.impl.harnesses.actor.contracts import HarnessProjection, _Record
from hyprial.kernel import HarnessLaunchSpec


def test_external_terminal_remains_external_and_session_fence_is_unchanged(composed):
    app, launcher, *_ = composed
    app.agents.create("terminal")
    actor = "agent:owner:node:terminal"
    domains = app._agent_session_domains
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        domains.call_session(RegisterSessionCommand(
            "register-external", actor, "/tmp", ("claude",), "claude-channel",
            "private-session", channel_confirmed=True,
            channel_protocol_version=CHANNEL_PROTOCOL_VERSION,
            channel_lease_token="lease-not-for-diagnostics-0123456789",
            process_pid=child.pid,
        ), SessionMutationCompleted)
        before = domains.session.read_session(actor)
        binding = domains.agent.read_binding(actor)
        cell = domains.cells.snapshot(actor)
        assert cell.custody == "external"
        assert cell.binding_revision == binding.revision
        assert "private-session" not in repr(dataclasses.asdict(cell))
        assert "lease-not-for-diagnostics" not in repr(dataclasses.asdict(cell))
        assert str(child.pid) not in repr(dataclasses.asdict(cell))
        domains.session.assert_owner(actor, "private-session")
        opened = domains.cells.open(actor)
        domains.cells.cancel(opened.observer_id)
        assert domains.cells.observe(actor, observer_id=opened.observer_id).status == "closed"
        domains.cells.close()
        assert child.poll() is None
        assert domains.session.read_session(actor) == before
        assert domains.agent.read_binding(actor) == binding
        domains.session.assert_owner(actor, "private-session")
        assert launcher.starts == 0
    finally:
        child.terminate()
        child.wait(timeout=3)


def test_new_binding_cannot_adopt_old_running_source(composed):
    app, launcher, harness, *_rest, spec = composed
    settle(app, LifecycleOperation.create("create", spec))
    domains = app._agent_session_domains
    old = domains.cells.snapshot(spec.actor)
    entity = domains.agent.read_agent("worker")
    domains.liveness.release(spec.actor)
    new_binding = domains.liveness.bind(spec.actor, harness="pi", runtime="headless")
    assert new_binding.revision != old.binding_revision
    assert harness.projection.read_observation_source("pi:worker", entity.entity_token,
                                                      new_binding.revision) is None
    new = domains.cells.snapshot(spec.actor)
    assert new.diagnostic_incarnation == old.diagnostic_incarnation
    assert new.lifecycle.state == "unknown" and not new.lifecycle.available
    assert domains.cells.observe(spec.actor, epoch=old.lifecycle.epoch,
                                 after_revision=old.lifecycle.revision).resync_required
    assert launcher.starts == 1


def test_bounded_mount_eviction_releases_only_observers():
    entities = {name: SimpleNamespace(uri=f"agent:o:host:{name}", actor=name, entity_token=f"secret-{name}")
                for name in ("a", "b")}
    agents = SimpleNamespace(read_agent=lambda name: entities.get(name.rsplit(":", 1)[-1]),
                             read_binding=lambda _: SimpleNamespace(revision=1, harness="pi", runtime="headless", session_id=None))
    projection = HarnessProjection()
    processes = [Process(), Process()]
    records = {f"pi:{name}": _Record(HarnessLaunchSpec("pi", name, True), process=process,
                observation_entity_token=f"secret-{name}", observation_binding_revision=1)
               for name, process in zip(entities, processes)}
    projection.publish(records, 1)
    cells = CellObservationView(agents, SimpleNamespace(read_session=lambda _: None), "host", capacity=1)
    cells.attach_harnesses(projection)
    try:
        a = cells.snapshot(entities["a"].uri)
        opened = cells.open(entities["a"].uri)
        cells.snapshot(entities["b"].uri)
        assert projection.lifecycle_observations.reader.observe(a.target, observer_id=opened.observer_id).status == "closed"
        assert len(projection.lifecycle_observations._bindings) == 1
        restored = cells.snapshot(entities["a"].uri)
        assert restored.diagnostic_incarnation == a.diagnostic_incarnation
        assert restored.lifecycle.epoch != a.lifecycle.epoch
        assert all(process.running for process in processes)
    finally:
        cells.close()
        projection.lifecycle_observations.close()


def test_foreign_canonical_uri_does_not_resolve_by_short_name(composed):
    app, *_ = composed
    app.agents.create("worker")
    cells = app._agent_session_domains.cells
    assert cells.snapshot("agent:other:node:worker") is None
    assert cells.snapshot("worker") is None
    with pytest.raises(ValueError, match="capacity"):
        CellObservationView(None, None, "host", capacity=0)
