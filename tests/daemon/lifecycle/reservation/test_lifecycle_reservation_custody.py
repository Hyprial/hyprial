"""Production composition cannot reject an accepted, still-committing reserve."""
from concurrent.futures import ThreadPoolExecutor
import threading

from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.composition import CorrelatedDomainEvents, HarnessPortClient
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.lifecycle_manager import LifecycleState
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import _Inbox, _Launcher, _LocalDelivery, _Transport, _lifecycle_create_operation


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections


def test_slow_durable_reservation_keeps_original_admission(tmp_path, monkeypatch):
    app = DaemonApplication(
        state_dir=tmp_path / "state", socket_path=tmp_path / "daemon.sock",
        hyprial_home=tmp_path / "home", node_id="node-1", owner="owner",
        zenoh_listen=("tcp/127.0.0.1:0",), zenoh_connect=(),
        **_app_injections(),
    )
    app.network_isolated = True
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(
        _Launcher(), event_sink=events,
        desired_state=app._state_persistence.settled_desired,
    )
    client = HarnessPortClient(actor, events)
    app._harnesses = client
    app._start_lifecycle_manager(
        transport=_Transport(), inbox=_Inbox(), local_delivery=_LocalDelivery(),
        harnesses=client,
    )
    manager = app._lifecycle_manager
    authority = app._state_persistence
    entered, release = threading.Event(), threading.Event()
    original = authority._journal_store.reserve
    calls = []

    def blocked(operation):
        calls.append(operation.operation_id)
        entered.set()
        assert release.wait(3)
        return original(operation)

    monkeypatch.setattr(authority._journal_store, "reserve", blocked)
    authority._call_timeout = 0.01
    operation = _lifecycle_create_operation("slow-reserve")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            admission = pool.submit(manager.submit, operation)
            assert entered.wait(1)
            assert not threading.Event().wait(0.05)
            assert not admission.done(), "accepted reserve was reported as a failure"
            release.set()
            assert admission.result(2) is PortAdmission.ACCEPTED
        assert manager.wait(operation.operation_id, 5).state is LifecycleState.COMPLETED
        assert calls == [operation.operation_id]
    finally:
        release.set()
        try:
            app._close()
        finally:
            app._close_home_lease()
