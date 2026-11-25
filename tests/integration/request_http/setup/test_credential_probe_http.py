"""Real HTTP→daemon→loopback model protocol, with no real credentials or vendor calls."""

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.integration.request_http.conftest import DaemonProcess, running_http

KEY = "isolated-http-probe-synthetic-key"


@contextmanager
def model_server(protocol):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            requests.append((self.path, {name.lower(): value for name, value in self.headers.items()}, body))
            if protocol == "slow":
                time.sleep(11)
            if protocol == "anthropic-messages":
                document = {"type": "message", "role": "assistant", "model": "loopback-model",
                            "content": [{"type": "text", "text": "isolated reply"}]}
            else:
                document = {"model": "loopback-model", "choices": [
                    {"message": {"role": "assistant", "content": "isolated reply"}}]}
            payload = json.dumps(document).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except BrokenPipeError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("protocol", ["openai-completions", "anthropic-messages"])
def test_probe_crosses_real_http_and_daemon_without_creating_agent_or_secret(isolation, protocol):
    daemon = DaemonProcess(isolation)
    daemon.start()
    try:
        with model_server(protocol) as (endpoint, requests), running_http(isolation) as http:
            body = {"provider": "custom", "secret": KEY, "model": "loopback-model",
                    "endpoint": endpoint, "protocol": protocol}
            path = "/daemon/request/agent.secret.provider-test"
            _, _, before = http.request("POST", "/daemon/request/agent.list", body={})
            status, _, result = http.request("POST", path, body=body, token="wrong-token")
            assert (status, result["dispatch"]) == (401, "not-dispatched")
            assert requests == []
            status, _, result = http.request("POST", path, body=dict(body, actor="pretend-owner"))
            assert status >= 400
            assert requests == []
            status, _, result = http.request("POST", path, body=body)
            assert (status, result["dispatch"]) == (200, "dispatched"), result
            assert result["result"]["outcome"] == "passed"
            assert len(requests) == 1
            request_path, headers, request_body = requests[0]
            assert request_path == ("/v1/messages" if protocol == "anthropic-messages" else "/v1/chat/completions")
            assert KEY in headers.get("authorization", headers.get("x-api-key", ""))
            assert request_body["model"] == "loopback-model"
            _, _, catalog = http.request("POST", "/daemon/request/agent.secret.provider-list", body={})
            assert catalog["result"]["entries"] == []
            _, _, after = http.request("POST", "/daemon/request/agent.list", body={})
            assert before["result"] == after["result"]
            assert not (isolation.home / "secrets" / "providers").exists()
            assert KEY not in json.dumps(result)
            assert "isolated reply" not in json.dumps(result)
            assert KEY not in http.log_path.read_text()
            assert KEY not in daemon.log_path.read_text()
    finally:
        daemon.stop()


def test_slow_upstream_returns_probe_timeout_before_default_http_budget(isolation):
    daemon = DaemonProcess(isolation)
    daemon.start()
    try:
        with model_server("slow") as (endpoint, requests), running_http(isolation) as http:
            status, _, result = http.request("POST", "/daemon/request/agent.secret.provider-test", body={
                "provider": "custom", "secret": KEY, "model": "loopback-model",
                "endpoint": endpoint, "protocol": "openai-completions",
            })
            assert status == 200, result
            assert result["dispatch"] == "dispatched"
            assert result["result"]["outcome"] == "timeout"
            assert len(requests) == 1
            assert KEY not in json.dumps(result)
    finally:
        daemon.stop()
