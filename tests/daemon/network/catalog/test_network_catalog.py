from __future__ import annotations

import json
import os
import threading

import pytest

from hyprial.daemon.impl.forwarding import ExposureStore, ForwardingSidecarError
from hyprial.daemon.impl.ipc.router import _IpcDispatchMixin
from hyprial.kernel import DaemonRequestError, ipc_errors

from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY


@pytest.mark.parametrize("name", ["network.expose", "network.unexpose", "network.exposures", "network.peerKey"])
def test_network_methods_are_bound_to_the_catalog(name):
    binding = METHOD_REGISTRY.get(name)
    assert binding is not None
    assert binding.spec.ipc_param_validation == "handler"
    assert binding.spec.http_exposed is False
    assert binding.spec.mutates is (name != "network.exposures")

class _Supervisor:
    def __init__(self):
        self.calls = []
        self.key_result = {"addr": "192.0.2.8:54321", "found": False}

    def expose(self, exposure, *, admission, keys=()):
        self.calls.append(("expose", exposure.copy(), admission, list(keys)))
        return exposure.copy()

    def unexpose(self, port):
        self.calls.append(("unexpose", port))

    def peer_key(self, address):
        self.calls.append(("peer_key", address))
        return self.key_result


class _Host(_IpcDispatchMixin):
    def __init__(self, path):
        self._restore_done = threading.Event()
        self._restore_done.set()
        self._inbox = self._harnesses = self._presence = object()
        self._exposure_store = ExposureStore(path)
        self._forwarding_supervisor = _Supervisor()
        self.starts = 0

    def _start_forwarding_supervisor(self):
        self.starts += 1


def test_exposures_preserves_extra_params_and_any_rejected_json(tmp_path):
    path = tmp_path / "exposures.json"
    path.write_text(json.dumps({"version": 1, "exposures": [42, None, "broken"]}))
    host = _Host(path)
    result = host.handle("network.exposures", {"ignored": [1, 2]})
    assert result["exposures"] == []
    assert [row["exposure"] for row in result["rejected"]] == [42, None, "broken"]
    assert host.starts == 0


def test_expose_unexpose_and_peer_key_keep_side_effects(tmp_path):
    host = _Host(tmp_path / "exposures.json")
    exposure = {"port": 8443, "target": "tcp:127.0.0.1:9443", "proxyProtocol": "none"}
    assert host.handle("network.expose", {**exposure, "ignored": True}) == {"exposure": exposure}
    assert host.handle("network.unexpose", {"port": 8444}) == {"port": 8444, "removed": True}
    address = "192.0.2.8:54321"
    assert host.handle("network.peerKey", {"addr": address}) == {"addr": address, "found": False}
    host._forwarding_supervisor.key_result = {"addr": address, "found": True, "key": {"raw": 1}}
    assert host.handle("network.peerKey", {"addr": address})["key"] == {"raw": 1}
    assert host.starts == 4


def test_a_new_exposure_starts_admitting_nobody(tmp_path):
    """Service grant exposing side: network.expose states its admission, and states nobody.

    The set is derived from the grants afterwards; until then the port must
    not admit anyone, so the expose the daemon sends is an empty key set and
    never "allow".
    """

    host = _Host(tmp_path / "exposures.json")
    exposure = {"port": 8443, "target": "tcp:127.0.0.1:9443", "proxyProtocol": "none"}

    host.handle("network.expose", exposure)

    assert host._forwarding_supervisor.calls == [("expose", exposure, "keys", [])]


def test_persist_failure_rolls_back_sidecar(tmp_path, monkeypatch):
    host = _Host(tmp_path / "exposures.json")
    def fail(_exposure):
        raise OSError("disk full")
    monkeypatch.setattr(host._exposure_store, "set", fail)
    with pytest.raises(OSError, match="disk full"):
        host.handle("network.expose", {"port": 8443, "target": "tcp:127.0.0.1:9443", "proxyProtocol": "none"})
    assert host._forwarding_supervisor.calls[-1] == ("unexpose", 8443)


@pytest.mark.parametrize("method,params,message", [
    ("network.expose", {"port": True}, "port must be between 1 and 65535"),
    ("network.expose", {"port": 7447, "target": ""}, "reserved for peer forwarding"),
    ("network.unexpose", {"port": False}, "port must be between 1 and 65535"),
    ("network.peerKey", {"addr": "invalid"}, "IP:port"),
])
def test_legacy_argument_refusals_are_kept(tmp_path, method, params, message):
    host = _Host(tmp_path / "exposures.json")
    with pytest.raises(DaemonRequestError) as caught:
        host.handle(method, params)
    assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    assert message in str(caught.value)
    assert host.starts == 0


def test_restore_gate_and_store_exception_are_preserved(tmp_path):
    host = _Host(tmp_path / "exposures.json")
    host._restore_done.clear()
    with pytest.raises(ipc_errors.DaemonRestoringError):
        host.handle("network.exposures", {})
    host._restore_done.set()
    host._exposure_store._path.write_text("broken")
    with pytest.raises(ForwardingSidecarError) as caught:
        host.handle("network.exposures", {})
    assert caught.value.code == "FORWARD_PROTOCOL"


def test_four_methods_over_isolated_unix_socket(tmp_path, monkeypatch):
    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.kernel import RequestPortError, daemon_request_once
    from hyprial.shell.impl.daemon_wiring import daemon_dependencies

    state = tmp_path / "state"
    state.mkdir()
    app = DaemonApplication(state_dir=state, socket_path=state / "daemon.sock",
        node_id="network-catalog-test", hyprial_home=tmp_path / "home", **daemon_dependencies())
    app._inbox = app._harnesses = app._presence = object()
    app._runtime = object()
    app._restore_done.set()
    app._forwarding_supervisor = _Supervisor()
    monkeypatch.setattr(app, "_start_forwarding_supervisor", lambda: None)
    monkeypatch.setattr(app, "_start_maintenance_scheduler", lambda: None)
    app._start_server()
    thread = threading.Thread(target=app._serve, daemon=True)
    thread.start()
    try:
        def call(name, params):
            return daemon_request_once(name, params, socket_path=app.socket_path, timeout=10)
        exposure = {"port": 8443, "target": "tcp:127.0.0.1:9443", "proxyProtocol": "none"}
        assert call("network.expose", exposure) == {"exposure": exposure}
        assert call("network.exposures", {"ignored": True})["exposures"] == [exposure]
        assert call("network.peerKey", {"addr": "192.0.2.8:54321"})["found"] is False
        assert call("network.unexpose", {"port": 8443}) == {"port": 8443, "removed": True}
        with pytest.raises(RequestPortError) as caught:
            call("network.expose", {"port": True})
        assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    finally:
        app.stop_event.set()
        app._server.close()
        if app._accept_reserve_fd is not None:
            os.close(app._accept_reserve_fd)
            app._accept_reserve_fd = None
        thread.join(timeout=5)
        app._close_ipc_clients()
        if app._ipc_request_owner is not None:
            app._ipc_request_owner.close(1.0)
        if app._dispatch_diagnostics is not None:
            app._dispatch_diagnostics.close(1.0)
        app._agent_session_domains.close()


@pytest.mark.parametrize("method,params", [
    ("network.expose", {"port": 8443, "target": "tcp:127.0.0.1:9443", "proxyProtocol": "none"}),
    ("network.unexpose", {"port": 8443}),
    ("network.peerKey", {"addr": "192.0.2.8:54321"}),
])
def test_sidecar_error_codes_remain_unchanged(tmp_path, method, params):
    host = _Host(tmp_path / "exposures.json")
    class Unsupported(_Supervisor):
        def expose(self, _, *, admission, keys=()):
            raise ForwardingSidecarError("SIDECAR_EXPOSE_UNSUPPORTED: old sidecar")
        def unexpose(self, _):
            raise ForwardingSidecarError("SIDECAR_EXPOSE_UNSUPPORTED: old sidecar")
        def peer_key(self, _):
            raise ForwardingSidecarError("SIDECAR_EXPOSE_UNSUPPORTED: old sidecar")
    host._forwarding_supervisor = Unsupported()
    with pytest.raises(DaemonRequestError) as caught:
        host.handle(method, params)
    assert caught.value.code == "SIDECAR_EXPOSE_UNSUPPORTED"
    assert str(caught.value) == "SIDECAR_EXPOSE_UNSUPPORTED: old sidecar"


def test_expose_preserves_target_and_protocol_validation(tmp_path):
    host = _Host(tmp_path / "exposures.json")
    for params, message in [
        ({"port": 8443, "target": "tcp:192.0.2.1:8000", "proxyProtocol": "none"}, "loopback"),
        ({"port": 8443, "target": "unix:/any", "proxyProtocol": "bad"}, "proxyProtocol must be v2 or none"),
    ]:
        with pytest.raises(DaemonRequestError) as caught:
            host.handle("network.expose", params)
        assert caught.value.code == ipc_errors.INVALID_ARGUMENT
        assert message in str(caught.value)
    assert host.starts == 0
