"""An old CREATE receipt cannot identify a new RESUME operation."""
from dataclasses import replace
from types import SimpleNamespace
import threading

import pytest

from hyprial.identity import desired_generation
from hyprial.daemon.impl.composition import AgentIdentityProjectionView, CorrelatedDomainEvents, HarnessPortClient
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.runtime.ports import ObserveHarnessRuntimeFactsCommand
from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation, LifecycleState
from hyprial.daemon.impl.processes.process_owner import ProcessOwner
from tests.integration.actor_preservation.lifecycle.test_actor_preserved_restore_threshold_states import HOUR_MS, _app, _close
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import _Transport, _LocalDelivery, _Inbox, _Presence
from ..quiesce.test_composition import Launcher


@pytest.mark.parametrize("mode", ["stopped", "running", "exited"])
def test_legacy_create_receipt_does_not_replay_or_identify_resume(tmp_path, monkeypatch, mode):
    running = mode == "running"
    app, spec = _app(tmp_path, now_ms=2_000_000_000_000, age_ms=17 * HOUR_MS)
    launcher, events = Launcher(), CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(launcher, event_sink=events, desired_state=app.desired_state,
                               agent_identity=AgentIdentityProjectionView(app._agent_session_domains))
    app._harnesses = HarnessPortClient(actor, events)
    app._inbox, app._presence = _Inbox(), _Presence()
    app._start_lifecycle_manager(transport=_Transport(), inbox=app._inbox,
                                local_delivery=_LocalDelivery(), harnesses=app._harnesses)
    try:
        assert app._classify_restore_specs((spec,)) == ()
        agent = app.agents.require("worker")
        disposition = app.agents.projection("worker").restore_disposition
        tail = f"worker:{agent.entity_token}:{disposition.disposition_token}:{desired_generation(spec)}"
        old_id, new_id = f"restore-wake:{tail}", f"resume-wake:{tail}"
        old = LifecycleOperation.create(old_id, app._lifecycle_spec(spec))
        app._lifecycle_manager.submit(old)
        old_result = app._lifecycle_manager.wait(old_id, 5)
        assert old_result.state is LifecycleState.COMPLETED, old_result
        if mode == "stopped":
            launcher.processes[0].release.set()
            stop = LifecycleOperation.deactivate("arranged-stop", replace(
                app._lifecycle_spec(spec), expected_entity_token=agent.entity_token))
            app._lifecycle_manager.submit(stop)
            assert app._lifecycle_manager.wait(stop.operation_id, 5).state is LifecycleState.COMPLETED
            assert not launcher.processes[0].running
            # DEACTIVATE also removes desired runtime state. This scenario
            # requests restoring the same spec, not clearing an obsolete wake.
            app.desired_state.upsert_harness(spec)
        elif mode == "exited":
            # External exit without a maintenance poll: ProcessOwner still
            # holds yesterday's running=True facts until an actual observation.
            launcher.processes[0].running = False
            launcher.processes[0].release.set()
        # Observe every later submission, including erroneous replays of old ID.
        submitted = []
        original = app._lifecycle_manager.submit
        def submit(operation):
            submitted.append(operation)
            return original(operation)
        monkeypatch.setattr(app._lifecycle_manager, "submit", submit)
        owner_threads, observer_threads = [], []
        native_observe, reconcile = ProcessOwner.observe_facts, actor._on_reconcile_session_refs
        def observe(process):
            observer_threads.append(threading.get_ident())
            return native_observe(process)
        def reconcile_on_owner(command, **kwargs):
            owner_threads.append(threading.get_ident())
            return reconcile(command, **kwargs)
        monkeypatch.setattr(ProcessOwner, "observe_facts", observe)
        monkeypatch.setattr(actor, "_on_reconcile_session_refs", reconcile_on_owner)
        monkeypatch.setattr(actor, "_complete_session_ref_reconcile", lambda *_:
                            pytest.fail("runtime observation persisted session refs"))
        # A cached status from before stop/start must not decide this upgrade.
        refreshed = []
        owner_call, owner_status = app._harnesses.call, app._harnesses.status
        def call(command, expected, **kwargs):
            answer = owner_call(command, expected, **kwargs)
            if isinstance(command, ObserveHarnessRuntimeFactsCommand):
                refreshed.append(command.correlation_id)
            return answer
        monkeypatch.setattr(app._harnesses, "call", call)
        monkeypatch.setattr(app._harnesses, "status", lambda: owner_status() if refreshed else (
            {"runtime": spec.harness, "name": spec.name, "running": not running},))
        assert app.agents.projection("worker").restore_disposition is not None
        assert app._wake_dormant_agent("worker", reason="legacy-upgrade")
        assert len(refreshed) == 1, "legacy observation reused cached runtime status"
        if mode != "stopped":
            assert observer_threads, "no native facts read was performed"
            assert not set(observer_threads) & {threading.get_ident(), *owner_threads}
        assert app._lifecycle_manager.result(old_id) == old_result
        if running:
            assert submitted == [], "running legacy CREATE was replayed/submitted again"
            assert len(launcher.processes) == 1
            with pytest.raises(KeyError):
                app._lifecycle_manager.result(new_id)
        else:
            assert [o.operation_id for o in submitted] == [new_id]
            assert submitted[0].kind.value == "resume"
            assert app._lifecycle_manager.result(new_id).state is LifecycleState.COMPLETED
            assert len(launcher.processes) == 2 and launcher.processes[-1].running
        assert app.agents.require("worker").entity_token == agent.entity_token
    finally:
        for process in launcher.processes:
            process.release.set()
        _close(app)


@pytest.mark.parametrize("fault", ["running", "compensating", "lookup-error",
                                  "wrong-receipt", "refresh-error", "inconsistent"])
def test_uncertain_legacy_operation_cannot_submit_resume_or_clear_disposition(tmp_path, monkeypatch, fault):
    app, spec = _app(tmp_path, now_ms=2_000_000_000_000, age_ms=17 * HOUR_MS)
    submitted, observed = [], []
    def result(operation_id):
        if fault == "lookup-error":
            raise OSError("receipt unavailable")
        state = {"running": LifecycleState.RUNNING, "compensating": LifecycleState.COMPENSATING,
                 "inconsistent": LifecycleState.FAILED}.get(fault, LifecycleState.COMPLETED)
        return SimpleNamespace(operation_id="other" if fault == "wrong-receipt" else operation_id,
                               state=state)
    def observe(*_):
        observed.append(1)
        if fault == "refresh-error":
            raise TimeoutError("harness owner observation pending")
    try:
        assert app._classify_restore_specs((spec,)) == ()
        before = app.agents.projection("worker").restore_disposition
        app._lifecycle_manager = SimpleNamespace(result=result)
        monkeypatch.setattr(app, "_run_lifecycle_operation", submitted.append)
        monkeypatch.setattr(app._harnesses, "call", observe, raising=False)
        monkeypatch.setattr(app._harnesses, "status", lambda: (
            {"runtime": spec.harness, "name": spec.name, "running": fault == "inconsistent"},),
            raising=False)
        assert not app._wake_dormant_agent("worker", reason="legacy-fault")
        assert not submitted, "unknown legacy outcome became a new operation"
        assert app.agents.projection("worker").restore_disposition == before
        if fault not in {"refresh-error", "inconsistent"}:
            assert not observed, "an unresolved/mismatched receipt reached runtime observation"
    finally:
        app._lifecycle_manager = None
        _close(app)


def test_process_owner_settles_new_facts_before_observation_returns():
    """Native observation updates the existing authority, not a second view."""
    class Native:
        running = True
        pid = None
        def stop(self):
            self.running = False
    native = Native()
    owner = ProcessOwner(native)
    try:
        assert owner.running is True
        native.running = False  # external exit, no harness projection involved
        observed = owner.observe_facts()
        assert observed.running is False
        assert owner.facts() == observed, "owner cache did not converge before returning evidence"
        assert owner.running is False, "native observation created a competing liveness view"
    finally:
        owner.stop()
