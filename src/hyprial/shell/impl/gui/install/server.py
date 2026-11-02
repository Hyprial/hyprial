"""Static server for the wheel's built GUI, using only the Python runtime.

Installed ``hyprial gui`` must not require Git, Node, or npm.  Vite's preview
server is therefore replaced by this small loopback server for the installed
asset path.  It implements the same health endpoint and the same explicit
501 "not wired" boundary as the source-mode Node placeholder; it does not
serve credentials and does not proxy to any external service.
"""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import sys
from typing import Any
from urllib.parse import urlsplit

from .resources import MANIFEST_NAME, GuiAssetError, load_assets

NOT_IMPLEMENTED_TEXT = "not implemented: hyprial backend not wired"
NOT_IMPLEMENTED_CODE = "BACKEND_NOT_WIRED"
HEALTH_PATH = "/__gui/healthz"
BUSINESS_PREFIX = "/api/gui/v1"


class GuiStaticServerError(RuntimeError):
    """The installed UI cannot be served safely."""


class _Interrupted(KeyboardInterrupt):
    """A signal that should stop the server with the conventional exit code."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


class _Handler(SimpleHTTPRequestHandler):
    server_version = "HyprialGUI/1"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path == HEALTH_PATH:
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "hyprial-gui",
                    "dataMode": "fixture",
                    "assets": self.server.gui_assets_status,
                    "assetsVerified": self.server.gui_assets_status == "installed",
                },
            )
            return
        if path == "/" or not path.startswith(BUSINESS_PREFIX):
            # A missing SPA route falls back to index.html, matching Vite.
            if path != "/" and not self._file_exists(path):
                self.path = "/index.html"
            return super().do_GET()
        self._not_wired("http.request", {"method": "GET", "path": path})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path.startswith(BUSINESS_PREFIX):
            self._not_wired("http.request", {"method": "POST", "path": path})
            return
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"ok": False, "code": "NOT_FOUND", "message": "not found"},
        )

    def _file_exists(self, path: str) -> bool:
        relative = path.lstrip("/")
        candidate = (Path(self.directory) / relative).resolve()
        root = Path(self.directory).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return False
        return candidate.is_file()

    def _not_wired(self, command: str, params: dict[str, Any]) -> None:
        body = json.dumps(
            {
                "ok": False,
                "code": NOT_IMPLEMENTED_CODE,
                "message": NOT_IMPLEMENTED_TEXT,
                "command": command,
                "params": params,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(HTTPStatus.NOT_IMPLEMENTED)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Hyprial-Backend", "not-wired")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(root: Path, host: str, port: int) -> ThreadingHTTPServer:
    root = root.expanduser().resolve()
    if not (root / "index.html").is_file():
        raise GuiStaticServerError(f"GUI index.html is missing under {root}")
    status = "explicit-unverified"
    if (root / MANIFEST_NAME).exists():
        try:
            load_assets(root, verify=True)
        except GuiAssetError as error:
            raise GuiStaticServerError(str(error)) from error
        status = "installed"
    handler = lambda *args, **kwargs: _Handler(  # noqa: E731 - http.server factory
        *args, directory=str(root), **kwargs
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.gui_assets_status = status
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the installed Hyprial GUI")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=4173, type=int)
    args = parser.parse_args(argv)
    try:
        server = create_server(args.root, args.host, args.port)
    except (OSError, GuiStaticServerError) as error:
        print(f"hyprial gui static server: {error}", file=sys.stderr)
        return 1
    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.signal(signum, _interrupt)
        except (OSError, ValueError):
            pass
    code = 0
    try:
        server.serve_forever()
    except _Interrupted as error:
        code = 128 + error.signum
    except KeyboardInterrupt:
        code = 130
    finally:
        server.server_close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return code


def _interrupt(signum: int, _frame: Any) -> None:
    raise _Interrupted(signum)


if __name__ == "__main__":
    raise SystemExit(main())
