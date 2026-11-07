"""Read-only endpoint diagnosis: budgets, redaction and actual HTTP cleanup."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hyprial.daemon.impl.harnesses.dsh import DshHttpApi
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.doctor.commands as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def _status(*endpoints):
    return {"connectors": [
        {"runtime": "dsh", "name": f"worker-{i}", "endpoint": endpoint}
        for i, endpoint in enumerate(endpoints)
    ]}


def test_duplicate_endpoint_probed_once_and_other_harness_ignored(monkeypatch):
    calls = []

    def describe(endpoint, **kwargs):
        calls.append(endpoint)
        return {"provider": "p", "model": "m"}

    monkeypatch.setattr(_cli_get_services(), "_dsh_host_describe", describe)
    status = _status("http://localhost:3080", "http://localhost:3080/")
    status["connectors"].append({"runtime": "claude", "endpoint": "http://not-probed"})
    assert _cli_owner_1._dsh_doctor_check(status)["status"] == "ok"
    assert calls == ["http://localhost:3080"]


def test_missing_endpoint_does_not_hide_an_unreachable_peer(monkeypatch):
    def describe(*args, **kwargs):
        raise RuntimeError("private error body")

    monkeypatch.setattr(_cli_get_services(), "_dsh_host_describe", describe)
    check = _cli_owner_1._dsh_doctor_check(_status(None, "http://localhost:3080"))
    assert check["status"] == "fail"
    assert check["metrics"]["missingEndpoint"] == ["worker-0"]
    assert "private error body" not in json.dumps(check)


def test_probe_budget_exhaustion_is_not_a_false_green(monkeypatch):
    now = [0.0]
    calls = []
    monkeypatch.setattr(_cli_get_services().time, "monotonic", lambda: now[0])

    def describe(endpoint, *, timeout_seconds):
        calls.append(timeout_seconds)
        now[0] += timeout_seconds
        return {"provider": "p", "model": "m"}

    monkeypatch.setattr(_cli_get_services(), "_dsh_host_describe", describe)
    check = _cli_owner_1._dsh_doctor_check(_status("http://a", "http://b", "http://c"))
    assert calls == [2.0, 2.0]
    assert check["status"] == "warn"
    assert check["metrics"]["unprobed"] == ["http://c"]
    assert check["metrics"]["reachable"] == ["http://a", "http://b"]


def test_diagnostic_redacts_endpoint_secrets_and_rpc_error_body(monkeypatch):
    def describe(*args, **kwargs):
        raise RuntimeError("SECRET_BODY_TOKEN")

    monkeypatch.setattr(_cli_get_services(), "_dsh_host_describe", describe)
    check = _cli_owner_1._dsh_doctor_check(_status("https://user:SECRET_PASSWORD@host/SECRET_PATH?SECRET_QUERY#SECRET_FRAGMENT"))
    rendered = json.dumps(check)
    assert "SECRET" not in rendered
    assert "https://host/[redacted-path]" in rendered
    assert check["status"] == "fail"


@pytest.mark.parametrize("description", [None, [], {}, {"provider": " ", "model": "m"}, {"provider": "p", "model": 2}])
def test_missing_configuration_is_not_healthy(monkeypatch, description):
    monkeypatch.setattr(_cli_get_services(), "_dsh_host_describe", lambda *args, **kwargs: description)
    assert _cli_owner_1._dsh_doctor_check(_status("http://localhost"))["status"] == "fail"


def test_probe_budget_cancels_a_blocked_dns_resolver(monkeypatch):
    released = threading.Event()

    class Resolver:
        def result(self, timeout):
            released.wait(3)
            raise RuntimeError("cancelled resolver")

        def cancel(self):
            released.set()

        def close(self):
            released.set()

    monkeypatch.setattr("hyprial.daemon.impl.harnesses.dsh.api._start_resolver", lambda *args: Resolver())
    started = time.monotonic()
    with pytest.raises(RuntimeError):
        _cli_get_services()._dsh_host_describe("http://blocked.invalid:3080", timeout_seconds=0.1)
    assert released.is_set()
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("mode", ["success", "error", "blocked"])
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_real_http_probe_only_describes_and_closes_transport(monkeypatch, mode, host):
    requests = []
    release = threading.Event()
    instances = []

    class Api(DshHttpApi):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, request["method"], request["payload"]))
            if mode == "blocked":
                release.wait(3)
                return
            self.send_response(200)
            self.end_headers()
            result = {"ok": True, "value": {"provider": "p", "model": "m"}} if mode == "success" else {"ok": False, "error": "private"}
            self.wfile.write(json.dumps({"rpcId": request["rpcId"], "result": result}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr("hyprial.daemon.DshHttpApi", Api)
    started = time.monotonic()
    try:
        endpoint = f"http://{host}:{server.server_port}"
        if mode == "success":
            assert _cli_get_services()._dsh_host_describe(endpoint, timeout_seconds=0.5) == {"provider": "p", "model": "m"}
        else:
            with pytest.raises(RuntimeError):
                _cli_get_services()._dsh_host_describe(endpoint, timeout_seconds=0.5)
        assert time.monotonic() - started < 2.0
        assert requests == [("/api/host.describe", "host.describe", {})]
        assert instances[0]._closed and instances[0].stopped()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
