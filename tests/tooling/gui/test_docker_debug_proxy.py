"""The Docker GUI proxy keeps assets and trusted session traffic on one origin."""

from __future__ import annotations

import http.client
import importlib.util
import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


PROXY_PATH = Path(__file__).resolve().parents[3] / "scripts/build/docker/gui-debug-proxy.py"


def load_proxy():
    spec = importlib.util.spec_from_file_location("gui_debug_proxy", PROXY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def start_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def drain_request_body(handler):
    """Consume a forwarded POST body before replying.

    BaseHTTPRequestHandler closes an HTTP/1.0 connection right after the
    response; if unread request bytes are still buffered the kernel answers
    with RST instead of FIN, so the proxy's upstream read raises
    ConnectionResetError and the test sees an intermittent 502.
    """
    try:
        remaining = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        remaining = 0
    while remaining > 0:
        chunk = handler.rfile.read(min(remaining, 65536))
        if not chunk:
            break
        remaining -= len(chunk)


def fetch(port, method, path, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    result = response.status, response.read(), response.getheaders()
    connection.close()
    return result


def test_proxy_keeps_static_and_session_requests_on_one_public_origin():
    seen = []

    class StaticHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(("static", self.path, self.headers.get("Cookie")))
            self.send_response(200)
            self.send_header("Set-Cookie", "untrusted=1")
            self.end_headers()
            self.wfile.write(b"GUI-ASSET")

        def log_message(self, *args):
            pass

    class SessionHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            drain_request_body(self)
            seen.append(("session", self.path, self.headers.get("Origin"), self.headers.get("Cookie")))
            self.send_response(200)
            self.send_header("Set-Cookie", "hyprial_session=opaque; HttpOnly; SameSite=Strict")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *args):
            pass

    static = start_server(StaticHandler)
    session = start_server(SessionHandler)
    proxy = None
    try:
        module = load_proxy()
        proxy = module.create_proxy(
            0,
            f"http://127.0.0.1:{static.server_port}",
            f"http://127.0.0.1:{session.server_port}",
        )
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        public = f"http://127.0.0.1:{proxy.server_port}"
        status, body, static_headers = fetch(
            proxy.server_port, "GET", "/assets/app.js", headers={"Cookie": "view=opaque"}
        )
        assert (status, body) == (200, b"GUI-ASSET")
        assert not any(name.lower() == "set-cookie" for name, _ in static_headers)
        status, body, headers = fetch(
            proxy.server_port,
            "POST",
            "/daemon/session",
            headers={"Host": f"127.0.0.1:{proxy.server_port}", "Origin": public, "Cookie": "view=opaque"},
            body=b"{}",
        )
        assert (status, body) == (200, b'{"ok":true}')
        assert ("Set-Cookie", "hyprial_session=opaque; HttpOnly; SameSite=Strict") in headers
        assert seen == [
            ("static", "/assets/app.js", None),
            ("session", "/daemon/session", f"http://127.0.0.1:{session.server_port}", "view=opaque"),
        ]
        status, _, _ = fetch(
            proxy.server_port,
            "POST",
            "/daemon/session",
            headers={"Host": f"127.0.0.1:{proxy.server_port}", "Origin": "http://evil.invalid"},
            body=b"{}",
        )
        assert status == 403
        assert len(seen) == 2
    finally:
        if proxy is not None:
            proxy.shutdown()
            proxy.server_close()
        static.shutdown()
        static.server_close()
        session.shutdown()
        session.server_close()


def test_handoff_rewrites_origin_without_exposing_grant(tmp_path, capsys):
    source = tmp_path / "source.url"
    target = tmp_path / "public.url"
    source.write_text("http://127.0.0.1:4383/#grant=private-value\n")
    module = load_proxy()
    module.write_handoff(source, target, "http://127.0.0.1:4382")
    assert target.read_text() == "http://127.0.0.1:4382/#grant=private-value\n"
    assert os.stat(target).st_mode & 0o777 == 0o600
    assert "private-value" not in capsys.readouterr().out


def test_container_ingress_accepts_public_port_and_rewrites_session_origin():
    seen = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            drain_request_body(self)
            seen.append((self.headers.get("Host"), self.headers.get("Origin")))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    upstream = start_server(Upstream)
    proxy = None
    try:
        module = load_proxy()
        proxy = module.create_proxy(
            0,
            f"http://127.0.0.1:{upstream.server_port}",
            f"http://127.0.0.1:{upstream.server_port}",
            public_origin="http://127.0.0.1:4382",
            listen_host="0.0.0.0",
        )
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        status, _, _ = fetch(
            proxy.server_port,
            "POST",
            "/daemon/session",
            headers={"Host": "127.0.0.1:4382", "Origin": "http://127.0.0.1:4382"},
            body=b"{}",
        )
        assert status == 200
        assert seen == [(f"127.0.0.1:{upstream.server_port}", f"http://127.0.0.1:{upstream.server_port}")]
        status, _, _ = fetch(
            proxy.server_port,
            "POST",
            "/daemon/session",
            headers={"Host": "127.0.0.1:4383", "Origin": "http://127.0.0.1:4382"},
            body=b"{}",
        )
        assert status == 403
    finally:
        if proxy is not None:
            proxy.shutdown()
            proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
