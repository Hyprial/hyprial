"""Opt-in evidence preserves the stop policy and presence admission contract."""

from dataclasses import asdict
import json
import signal
import threading
import time
from types import SimpleNamespace

import pytest

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.application import DaemonApplication, _StopRequestTrace
from hyprial.daemon.impl.inbox import DeliveryCustodyCoordinator, DeliveryCustodyFacade, MemoryDeliveryTransport
from hyprial.daemon.impl.transport.api import TransportSample
from hyprial.daemon.impl.transport.keys import KeySpace
from hyprial.daemon.impl.transport.zenoh.presence import LivelinessDirectory
from tests.daemon.application.restore.test_daemon_restore_gate import _FakeHarnesses, _FakePresence, _IdleRuntime, _InboxEvents, state_root as state_root

from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections


class PresenceSession:
    def get_liveliness(self, key, *, timeout=1.0):
        return []

    def on_rebuild(self, callback):
        return lambda: None

    def observe_liveliness(self, key, callback, *, history=True):
        return SimpleNamespace(close=lambda: None)

    def projection(self):
        return SimpleNamespace(generation=3, callbacks_complete=True, closed=False)


def test_presence_trace_is_payload_free_and_does_not_admit_stale_sample():
    events = []
    directory = LivelinessDirectory(PresenceSession(), observation_sink=events.append)
    key = KeySpace().actor_liveliness("host.example")
    try:
        directory._actor_event(TransportSample(key, b"secret payload", generation=2))
        assert events[-1].admission == "stale"
        assert not directory.online_actors()
        directory._actor_event(TransportSample(key, b"secret payload", generation=3))
        deadline = time.monotonic() + 1
        while not directory.actor_online("host.example") and time.monotonic() < deadline:
            time.sleep(0.005)
        assert directory.actor_online("host.example")
        assert events[-1].admission == "accepted"
        assert events[-1].transport_generation == 3
        assert "payload" not in json.dumps([asdict(event) for event in events])
    finally:
        directory.close()


def test_trace_failure_cannot_hide_presence_refusal_or_invalidate_success(monkeypatch):
    def broken_trace(_event):
        raise RuntimeError("secret diagnostic exception")

    directory = LivelinessDirectory(PresenceSession(), observation_sink=broken_trace)
    sample = TransportSample(KeySpace().actor_liveliness("host.example"), b"", generation=3)
    try:
        directory._actor_event(sample)
        assert directory.presence_projection().complete
        monkeypatch.setattr(directory._authority, "change", lambda *_a, **_kw: AdmissionResult.OVERLOADED)
        with pytest.raises(RuntimeError, match="presence admission overloaded"):
            directory._actor_event(sample)
    finally:
        directory.close()


def test_stop_callback_keeps_event_identity_and_records_signal(monkeypatch):
    app = DaemonApplication.__new__(DaemonApplication)
    original = threading.Event()
    app.stop_event = original
    app._stop_request_trace = _StopRequestTrace()
    home_lost = app._stop_request_trace.bind(original, "home-ownership-lost")
    app.stop_event = threading.Event()
    home_lost()
    assert original.is_set()
    assert not app.stop_event.is_set()
    assert app._stop_request_trace.last == ("home-ownership-lost", None)
    handlers = {}
    monkeypatch.setattr(signal, "getsignal", lambda _signum: signal.SIG_DFL)
    monkeypatch.setattr(signal, "signal", lambda signum, handler: handlers.__setitem__(signum, handler))
    app._install_signal_handlers()
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert app.stop_event.is_set()
    assert app._stop_request_trace.last == ("signal", signal.SIGTERM)


@pytest.mark.parametrize("failure", [False, True])
def test_run_records_stop_or_failure_without_exception_values(state_root, monkeypatch, failure):
    (state_root / "home").mkdir(mode=0o700)
    app = DaemonApplication(
        state_dir=state_root / "state", socket_path=state_root / "daemon.sock",
        node_id="trace-test", hyprial_home=state_root / "home",
        zenoh_listen=("tcp/127.0.0.1:0",), zenoh_connect=(),
        **_app_injections(),
    )
    app.network_isolated = True
    database = state_root / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(database, MemoryDeliveryTransport(), _InboxEvents())
    app._inbox_coordinator = coordinator
    app._inbox = DeliveryCustodyFacade(coordinator, database)
    app._harnesses = _FakeHarnesses()
    app._presence = _FakePresence()
    captured = []
    monkeypatch.setattr(app, "_log", lambda level, component, event, **data: captured.append((event, data)))
    monkeypatch.setattr(app, "_start_runtime", lambda: setattr(app, "_runtime", _IdleRuntime()))
    monkeypatch.setattr(app, "_restore_adapters", lambda: None)

    def serve():
        if failure:
            raise RuntimeError("private exception value must not be exported")
        app.handle("shutdown", {})

    monkeypatch.setattr(app, "_serve", serve)
    try:
        if failure:
            with pytest.raises(RuntimeError, match="private exception value"):
                app.run()
        else:
            app.run()
    finally:
        app._close()
        app._home_guard.close()
    stopping = [data for event, data in captured if event == "daemon.stopping"]
    assert len(stopping) == 1
    data = stopping[0]
    assert data["runFailed"] is failure
    if failure:
        assert data["failureType"] == "RuntimeError"
        assert data["failureSite"].endswith(":serve")
    else:
        assert data["stopRequestSource"] == "ipc-shutdown"
        assert data["failureType"] is None
    assert "private exception value" not in json.dumps(data)
    assert "keepaliveSeconds" in data["homeLease"]


def test_presence_snapshot_disabled_and_failure_are_observational():
    app = DaemonApplication.__new__(DaemonApplication)
    # Disabled by default: there need not even be a composed transport.
    app._trace_presence_snapshot(None)
    app._trace_presence_enabled = True
    app._transport = SimpleNamespace(projection=lambda: (_ for _ in ()).throw(RuntimeError("secret")))
    hosts = ["host.example"]
    app._trace_presence_snapshot(hosts)
    assert hosts == ["host.example"]
