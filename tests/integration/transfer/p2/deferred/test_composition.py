"""Real identity/lifecycle/harness owners; process and transport edges are fake."""
from hyprial.daemon.impl.composition import AgentIdentityProjectionView, CorrelatedDomainEvents, HarnessPortClient
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.lifecycle_manager import LifecycleState
from tests.integration.actor_preservation.lifecycle.test_actor_preserved_restore_threshold_states import HOUR_MS, _app, _close
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import _Transport, _LocalDelivery, _Inbox, _Presence
from ..quiesce.test_composition import Launcher


def test_deferred_source_recovery_reaches_existing_lifecycle_owner(tmp_path, monkeypatch):
    app, spec = _app(tmp_path, now_ms=2_000_000_000_000, age_ms=HOUR_MS)
    launcher, events = Launcher(), CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(
        launcher, event_sink=events, desired_state=app.desired_state,
        agent_identity=AgentIdentityProjectionView(app._agent_session_domains),
        source_custody_clear=lambda name, token: app._transfer_source_state(name, token).state == "clear",
    )
    app._harnesses = HarnessPortClient(actor, events)
    app._inbox, app._presence = _Inbox(), _Presence()
    app._start_lifecycle_manager(transport=_Transport(), inbox=app._inbox,
                                local_delivery=_LocalDelivery(), harnesses=app._harnesses)
    try:
        original = app._transfer_p2._store.journal.custody_snapshot
        token = app.agents.require("worker").entity_token
        def fail(*_):
            raise OSError("injected read fault")
        monkeypatch.setattr(app._transfer_p2._store.journal, "custody_snapshot", fail)
        assert app._classify_restore_specs((spec,)) == ()
        assert not launcher.processes
        monkeypatch.setattr(app._transfer_p2._store.journal, "custody_snapshot", original)
        app._restore_wake_scan(0)
        pending = next(iter(app._transfer_deferred.values()))
        assert pending.operation is not None
        receipt = app._lifecycle_manager.wait(pending.operation, 5)
        assert receipt.state is LifecycleState.COMPLETED, receipt
        assert len(launcher.processes) == 1 and launcher.processes[0].running
        assert app.agents.require("worker").entity_token == token
        app._restore_wake_scan(0)
        assert not app._transfer_deferred
        app._restore_wake_scan(0)
        assert len(launcher.processes) == 1
    finally:
        for process in launcher.processes:
            process.release.set()
        _close(app)
