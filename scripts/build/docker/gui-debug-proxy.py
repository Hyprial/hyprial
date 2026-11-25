"""Loopback asset/session proxy and private public-origin handoff for GUI debug."""

from __future__ import annotations

import argparse
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit


HOP_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "origin",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
MAX_REQUEST_BYTES = 128 * 1024


def _loopback_origin(value: str) -> tuple[str, int]:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("upstream must be an http://127.0.0.1:<port> origin")
    return parsed.hostname, parsed.port


def create_proxy(
    port: int,
    static_origin: str,
    session_origin: str,
    *,
    public_origin: str | None = None,
    listen_host: str = "127.0.0.1",
) -> ThreadingHTTPServer:
    static_host, static_port = _loopback_origin(static_origin)
    session_host, session_port = _loopback_origin(session_origin)
    if not 0 <= port <= 65535:
        raise ValueError("port must be 0-65535")
    if listen_host not in {"127.0.0.1", "0.0.0.0"}:
        raise ValueError("listen host must be an IPv4 loopback or wildcard address")
    if public_origin is not None:
        _loopback_origin(public_origin)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            self._forward()

        def do_HEAD(self) -> None:
            self._forward()

        def do_POST(self) -> None:
            self._forward()

        def _forward(self) -> None:
            public = public_origin or f"http://127.0.0.1:{self.server.server_port}"
            if self.headers.get_all("Host") != [urlsplit(public).netloc]:
                self.send_error(403)
                return
            if not self.path.startswith("/") or self.path.startswith("//"):
                self.send_error(400)
                return
            origin = self.headers.get("Origin")
            if origin is not None and origin != public:
                self.send_error(403)
                return
            session_route = self.path.startswith("/daemon/") or self.path == "/__gui/healthz"
            if self.command not in {"GET", "HEAD"} and (
                not session_route or origin != public
            ):
                self.send_error(403)
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if content_length < 0 or content_length > MAX_REQUEST_BYTES:
                self.send_error(413)
                return
            if self.headers.get("Transfer-Encoding"):
                self.send_error(400)
                return
            body = self.rfile.read(content_length) if content_length else None
            host, upstream_port = (
                (session_host, session_port) if session_route else (static_host, static_port)
            )
            upstream_origin = f"http://{host}:{upstream_port}"
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in HOP_HEADERS and not name.lower().startswith("x-forwarded-")
                and (session_route or name.lower() != "cookie")
            }
            headers["Host"] = f"{host}:{upstream_port}"
            if origin is not None:
                headers["Origin"] = upstream_origin
            try:
                connection = HTTPConnection(host, upstream_port, timeout=10)
                connection.request(self.command, self.path, body=body, headers=headers)
                response = connection.getresponse()
                payload = response.read()
                self.send_response_only(response.status, response.reason)
                for name, value in response.getheaders():
                    if name.lower() not in HOP_HEADERS and (
                        session_route or name.lower() != "set-cookie"
                    ):
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
                connection.close()
            except (OSError, TimeoutError):
                self.send_error(502)

    return ThreadingHTTPServer((listen_host, port), Handler)


def write_handoff(source: Path, target: Path, public_origin: str) -> None:
    _loopback_origin(public_origin)
    private_url = source.read_text(encoding="utf-8").strip()
    parsed = urlsplit(private_url)
    _loopback_origin(f"{parsed.scheme}://{parsed.netloc}")
    grants = parse_qs(parsed.fragment, keep_blank_values=True).get("grant", [])
    if len(grants) != 1 or not grants[0]:
        raise ValueError("source handoff must contain exactly one launch grant")
    public = urlsplit(public_origin)
    launch_url = urlunsplit((public.scheme, public.netloc, parsed.path, parsed.query, parsed.fragment))
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(f"{launch_url}\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument("--static-origin", required=True)
    serve.add_argument("--session-origin", required=True)
    serve.add_argument("--public-origin")
    serve.add_argument("--listen-host", default="127.0.0.1")
    handoff = commands.add_parser("handoff")
    handoff.add_argument("--source", type=Path, required=True)
    handoff.add_argument("--target", type=Path, required=True)
    handoff.add_argument("--public-origin", required=True)
    args = parser.parse_args()
    if args.command == "serve":
        server = create_proxy(
            args.port,
            args.static_origin,
            args.session_origin,
            public_origin=args.public_origin,
            listen_host=args.listen_host,
        )
        try:
            server.serve_forever()
        finally:
            server.server_close()
    else:
        write_handoff(args.source, args.target, args.public_origin)


if __name__ == "__main__":
    main()
