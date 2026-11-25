"""The text TOP projection gets metrics from the daemon's snapshot."""

import threading
from pathlib import Path
from uuid import uuid4

import pytest

from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY, MIGRATED_METHODS
from tests.daemon.ipc.contracts.agent.test_relations_specs import app as app
from tests.daemon.workers.test_worker_relay import isolated as isolated
from tests.integration.service_connect.test_service_connect_integration import _IdleRuntime


@pytest.fixture()
def top_socket(app):
    app.socket_path = Path("/tmp") / f"top-{uuid4().hex}.sock"
    app._runtime = _IdleRuntime()
    app._maintenance_generation = 1
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        yield app
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(2.0)
        app.socket_path.unlink(missing_ok=True)
        assert not dispatcher.is_alive()


def test_top_inspect_is_a_safe_ipc_only_registered_read():
    binding = METHOD_REGISTRY.get("top.inspect")
    assert binding is not None
    assert "top.inspect" in MIGRATED_METHODS
    assert (binding.spec.mutates, binding.spec.retry, binding.spec.http_exposed) == (False, "safe", False)


def test_process_metrics_are_per_row_and_snapshot_is_not_mutated(monkeypatch):
    from copy import deepcopy
    from hyprial.daemon.impl.operations import top

    snapshot = {"daemon": {}, "actors": [{"pid": 11}, {"pid": None}], "stranded": [], "quota": {}}
    original = deepcopy(snapshot)
    monkeypatch.setattr(top, "_sample_process_table", lambda: {
        11: (1, 1.0, 10, 2.0, 30.0), 12: (11, 0.5, 20, 3.0, 10.0),
    })

    class App:
        def _ipc_top_snapshot(self):
            return snapshot

    inspected = METHOD_REGISTRY.invoke(App(), "top.inspect", {})
    assert inspected["actors"][0]["processMetrics"] == {
        "cpuPercent": 1.5, "rssKb": 30, "cpuSeconds": 5.0, "elapsedSeconds": 30.0}
    assert inspected["actors"][1]["processMetrics"] is None
    assert snapshot == original
    assert METHOD_REGISTRY.invoke(App(), "top.snapshot", {}) == original


def test_top_inspect_rejects_arbitrary_pid():
    import pytest
    from hyprial.kernel import DaemonRequestError

    with pytest.raises(DaemonRequestError) as error:
        METHOD_REGISTRY.invoke(object(), "top.inspect", {"pid": 123})
    assert error.value.code == "INVALID_ARGUMENT"


def test_ps_timeout_and_os_failure_return_missing_metrics(monkeypatch):
    import subprocess
    from hyprial.daemon.impl.operations import top

    snapshot = {"daemon": {}, "actors": [{"pid": 11}], "stranded": [], "quota": {}}
    for failure in (subprocess.TimeoutExpired("ps", 5), OSError("unavailable")):
        def fail(*args, **kwargs):
            raise failure
        monkeypatch.setattr(top.subprocess, "run", fail)
        assert top.inspect_snapshot(snapshot)["actors"][0]["processMetrics"] is None


def test_slow_process_sample_does_not_block_ping(top_socket, monkeypatch):
    import threading
    from hyprial.daemon.impl.operations import top
    from tests.daemon.workers.test_worker_relay import frame, request

    entered = threading.Event()
    release = threading.Event()
    responses = []
    snapshot = {"daemon": {}, "actors": [], "stranded": [], "quota": {}}
    monkeypatch.setattr(top_socket, "_ipc_top_snapshot", lambda: snapshot)

    def sample():
        entered.set()
        assert release.wait(5)
        return {}

    monkeypatch.setattr(top, "_sample_process_table", sample)
    worker = threading.Thread(target=lambda: responses.append(request(top_socket.socket_path, frame("top.inspect", {}))))
    worker.start()
    try:
        assert entered.wait(5)
        assert request(top_socket.socket_path, frame("ping", {}))["result"]["running"] is True
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert responses[0]["result"] == snapshot



def test_worker_relay_does_not_gain_top_inspect(isolated):
    from tests.daemon.workers.test_worker_relay import frame, request

    _, _, relay, _ = isolated
    assert request(relay.socket_path, frame("top.inspect", {}))["error"]["code"] == "METHOD_NOT_FOUND"


def test_real_socket_samples_the_daemon_host_process(top_socket, monkeypatch):
    import os
    from tests.daemon.workers.test_worker_relay import frame, request

    snapshot = {"daemon": {}, "actors": [{"pid": os.getpid()}], "stranded": [], "quota": {}}
    monkeypatch.setattr(top_socket, "_ipc_top_snapshot", lambda: snapshot)
    result = request(top_socket.socket_path, frame("top.inspect", {}))["result"]
    metrics = result["actors"][0]["processMetrics"]
    assert metrics is not None
    assert set(metrics) == {"cpuPercent", "rssKb", "cpuSeconds", "elapsedSeconds"}
    assert "processMetrics" not in snapshot["actors"][0]


def test_restore_refuses_before_sampling(top_socket, monkeypatch):
    from hyprial.daemon.impl.operations import top
    from tests.daemon.workers.test_worker_relay import frame, request

    calls = []
    monkeypatch.setattr(top, "_sample_process_table", lambda: calls.append(True))
    top_socket._restore_done.clear()
    try:
        result = request(top_socket.socket_path, frame("top.inspect", {}))
        assert result["error"]["code"] == "DAEMON_RESTORING"
        assert calls == []
    finally:
        top_socket._restore_done.set()


def test_formal_error_examples_match_real_socket(top_socket, monkeypatch):
    from hyprial.daemon.impl.request_catalog.registry import daemon_openapi
    from tests.daemon.workers.test_worker_relay import frame, request

    catalog = {entry["name"]: entry for entry in daemon_openapi()["x-hyprial-methods"]}
    cases = {case["name"]: case for case in catalog["top.inspect"]["x-hyprial-examples"]}
    for name in ("schema-invalid", "service-not-ready"):
        case = cases[name]
        if name == "service-not-ready":
            monkeypatch.setattr(top_socket, "_ipc_top_snapshot", None)
        result = request(top_socket.socket_path, frame("top.inspect", case["params"]))
        assert result["error"] == case["error"]
