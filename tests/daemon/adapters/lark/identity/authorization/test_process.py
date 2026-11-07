"""Real official SDK and owned process, talking only to a loopback stand-in."""

import base64
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from hyprial.daemon import create_lark_authorization_service
from hyprial.kernel import create_owned_process_group
from hyprial.daemon.impl.adapters.lark.credentials.authorization.process import AuthorizationEffect
from tests.daemon.adapters.lark.identity.authorization.test_service import home as home


class Standin:
    def __init__(self):
        self.approve = threading.Event()
        self.release = threading.Event()
        self.hung = threading.Event()
        self.fail = None
        self.hang = None
        self.malformed = False
        self.granted = False
        self.actions = []

    def response(self, path, form):
        if self.hang and (self.hang == path or form.get("action", [None])[0] == self.hang):
            self.hung.set()
            self.release.wait(5)
        if path.endswith("tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": "fixture-tenant-token"}
        if path.endswith("application/v6/scopes"):
            if self.malformed:
                return {"code": 0}
            return {"code": 0, "data": {"scopes": [
                {"scope_name": name, "scope_type": "tenant", "grant_status": int(self.granted)}
                for name in ("im:message", "im:message.group_at_msg:readonly")
            ]}}
        action = form.get("action", [None])[0]
        self.actions.append(action)
        if action == "init":
            return {"supported_auth_methods": ["client_secret"]}
        if action == "begin":
            return {"device_code": "fixture-device-secret", "interval": 0.03, "expires_in": 60,
                    "verification_uri_complete": "https://accounts.feishu.cn/authorize?user_code=fixture"}
        if action == "poll":
            if self.fail:
                return {"error": self.fail, "error_description": "fixture-error-secret"}
            if not self.approve.is_set():
                return {"error": "authorization_pending"}
            self.granted = True
            return {"client_id": "cli_test", "client_secret": "fixture-sdk-secret"}
        raise AssertionError(f"unexpected stand-in path {path}")


@pytest.fixture
def standin(monkeypatch):
    state = Standin()

    class Handler(BaseHTTPRequestHandler):
        def respond(self, form):
            body = json.dumps(state.response(self.path, form)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.respond(parse_qs(body.decode()))

        def do_GET(self):  # noqa: N802
            self.respond({})

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("HYPRIAL_LARK_ACCOUNTS_DOMAIN", origin)
    monkeypatch.setenv("HYPRIAL_LARK_BASE_URL", origin)
    monkeypatch.setenv("HYPRIAL_LARK_APP_SECRET", "ambient-secret-must-not-be-inherited")
    yield state
    state.release.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def wait_status(service, identity, status):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        result = service.operation(identity)
        if result["status"] == status:
            return result
        time.sleep(0.02)
    pytest.fail(f"did not reach {status}: {result}")


def test_real_sdk_callback_and_independent_grants_never_expose_secrets(home, tmp_path, standin):
    effects = []

    def registrar(job):
        result = AuthorizationEffect(job)
        effects.append(result)
        return result

    service = create_lark_authorization_service(home, tmp_path, registrar=registrar)
    try:
        initial = service.status("test")
        assert initial["authorization"] == "unauthorized"
        op = service.start("test")
        pending = wait_status(service, op["operationId"], "awaiting_user")
        query = parse_qs(urlsplit(pending["verificationUrl"]).query)
        assert query["clientID"] == ["cli_test"]
        assert "createOnly" not in query and "app_preset" not in query
        encoded = query["addons"][0]
        addons = json.loads(gzip.decompress(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))))
        assert addons["scopes"]["tenant"] == ["im:message", "im:message.group_at_msg:readonly"]
        standin.approve.set()
        completed = wait_status(service, op["operationId"], "succeeded")
        assert completed["authorization"]["authorization"] == "authorized"
        assert "message-send" in completed["authorization"]["capabilities"]
        assert isinstance(completed["authorization"]["checkedAt"], (int, float))
        assert not any(secret in json.dumps([initial, pending, completed]) for secret in (
            "fixture-secret-never-return", "fixture-sdk-secret", "fixture-device-secret", "fixture-tenant-token"))
    finally:
        service.close()
    assert all(effect._process.poll() is not None and not effect._root.exists() for effect in effects)


def test_worker_environment_excludes_ambient_credentials(home, tmp_path, standin, monkeypatch):
    captured = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        captured.append(kwargs["env"])
        return original(*args, **kwargs)

    monkeypatch.setattr("hyprial.daemon.impl.adapters.lark.credentials.authorization.process.subprocess.Popen", spawn)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-ai-credential")
    monkeypatch.setenv("HYPRIAL_LARK_APP_ID", "ambient-app")
    service = create_lark_authorization_service(home, tmp_path)
    try:
        assert service.status("test")["authorization"] == "unauthorized"
    finally:
        service.close()
    assert captured
    assert all("HYPRIAL_LARK_APP_SECRET" not in env and "OPENAI_API_KEY" not in env
               and "HYPRIAL_LARK_APP_ID" not in env and env["HOME"] != str(home)
               for env in captured)


@pytest.mark.parametrize("code,status", [("access_denied", "denied"), ("expired_token", "expired")])
def test_real_sdk_typed_failures_preserve_prior_grants(home, tmp_path, standin, code, status):
    standin.granted, standin.fail = True, code
    service = create_lark_authorization_service(home, tmp_path)
    try:
        assert service.status("test")["authorization"] == "authorized"
        op = service.start("test")
        result = wait_status(service, op["operationId"], status)
        assert result["authorization"]["authorization"] == "authorized"
        assert "fixture-error-secret" not in str(result)
    finally:
        service.close()


def test_real_hung_sdk_is_cancelled_and_reaped(home, tmp_path, standin):
    standin.hang = "init"
    service = create_lark_authorization_service(home, tmp_path)
    try:
        op = service.start("test")
        assert standin.hung.wait(8)
        before = time.monotonic()
        assert service.cancel(op["operationId"])["status"] == "cancelled"
        service.close()
        assert time.monotonic() - before < 2
    finally:
        standin.release.set()
        service.close()


def test_inventory_network_deadline_does_not_leave_a_worker(home, tmp_path, standin):
    standin.hang = "/open-apis/application/v6/scopes"
    service = create_lark_authorization_service(home, tmp_path, inventory_timeout=3)
    try:
        before = time.monotonic()
        result = service.status("test")
        assert standin.hung.is_set()
        assert result["authorization"] == "unconfirmed"
        assert time.monotonic() - before < 5
    finally:
        standin.release.set()
        service.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group orphan probe")
@pytest.mark.parametrize("binding_enabled", [True, False], ids=["bound", "disabled-control"])
def test_host_crash_stops_a_worker_blocked_in_sdk_io(home, tmp_path, standin, binding_enabled):
    standin.hang = "init"
    script = """
import json, subprocess, sys
from hyprial.daemon.impl.adapters.lark.credentials.authorization.process import AuthorizationEffect
if sys.argv[3] == 'disabled':
    spawn = subprocess.Popen
    def without_binding(argv, **kwargs):
        mutation = ('from hyprial.daemon.impl.adapters.lark.credentials.authorization import worker; '
                    'worker.bind_current_process_to_parent = lambda *args, **kwargs: lambda: None; worker.main()')
        return spawn([argv[0], '-c', mutation, argv[-1]], **kwargs)
    subprocess.Popen = without_binding
effect = AuthorizationEffect({'action':'authorize', 'home':sys.argv[1], 'stateDir':sys.argv[2], 'adapter':'test', 'appId':'cli_test'})
print(json.dumps({'pid':effect._process.pid, 'root':str(effect._root)}), flush=True)
sys.stdin.readline()
"""
    parent = subprocess.Popen([sys.executable, "-c", script, str(home), str(tmp_path),
                               "enabled" if binding_enabled else "disabled"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    owner = create_owned_process_group(label="authorization orphan test")
    try:
        record = json.loads(parent.stdout.readline())
        owner.register(record["pid"])
        assert standin.hung.wait(8)
        parent.kill()
        parent.wait(timeout=3)
        deadline = time.monotonic() + 3
        while not owner.stopped() and time.monotonic() < deadline:
            time.sleep(0.02)
        from pathlib import Path
        if binding_enabled:
            assert owner.stopped(), "orphaned authorization worker survived its host"
            assert not Path(record["root"]).exists()
        else:
            assert not owner.stopped(), "disabled binding must fail the orphan-stop oracle"
    finally:
        owner.force_close()
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=3)
        for pipe in (parent.stdin, parent.stdout, parent.stderr):
            pipe.close()
        standin.release.set()
        if "record" in locals():
            shutil.rmtree(record["root"], ignore_errors=True)
