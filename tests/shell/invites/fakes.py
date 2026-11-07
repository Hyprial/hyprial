"""A fake Casdoor account server on 127.0.0.1 for the invite-pickup tests.

Implements exactly the surface ``CasdoorAccountClient`` uses
(``/api/get-account``, ``/api/update-user?id=<owner>/<name>&columns=properties``)
against a real HTTP server, so the client's default urllib transport is
exercised for real.  Everything stays in memory; the port is OS-assigned.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeCasdoor:
    """One in-memory account behind a real loopback HTTP server."""

    def __init__(self) -> None:
        self.account: dict[str, Any] = {
            "owner": "test-org",
            "name": "alice",
            "properties": {},
        }
        # Test knobs: flip to simulate server-side failures.
        self.account_status = 200
        self.update_status = 200
        # Evidence: what the client actually sent.
        self.authorization_headers: list[str] = []
        self.user_agents: list[str] = []
        self.update_calls: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:  # keep test output quiet
                return

            def _json(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                fake.authorization_headers.append(
                    self.headers.get("Authorization", "")
                )
                fake.user_agents.append(self.headers.get("User-Agent", ""))
                if self.path == "/api/get-account":
                    if fake.account_status != 200:
                        self._json(fake.account_status, {"status": "error"})
                        return
                    self._json(200, {"status": "ok", "data": fake.account})
                    return
                self._json(404, {"status": "error", "msg": "not found"})

            def do_POST(self) -> None:
                fake.authorization_headers.append(
                    self.headers.get("Authorization", "")
                )
                fake.user_agents.append(self.headers.get("User-Agent", ""))
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/api/update-user":
                    length = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(length)
                    query = urllib.parse.parse_qs(parsed.query)
                    record: dict[str, Any] = {
                        "id": query.get("id", [None])[0],
                        "columns": query.get("columns", [None])[0],
                        "body": json.loads(body) if body else None,
                    }
                    fake.update_calls.append(record)
                    if fake.update_status != 200:
                        self._json(fake.update_status, {"status": "error"})
                        return
                    if isinstance(record["body"], dict) and isinstance(
                        record["body"].get("properties"), dict
                    ):
                        fake.account["properties"] = record["body"]["properties"]
                    self._json(200, {"status": "ok"})
                    return
                self._json(404, {"status": "error", "msg": "not found"})

        return Handler

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def set_invites_property(self, items: list[dict[str, Any]] | str) -> None:
        """Seed ``properties.hyprialInvites`` (a JSON string, as Casdoor stores it)."""
        value: str = items if isinstance(items, str) else json.dumps(items)
        self.account["properties"]["hyprialInvites"] = value

    def stored_invites(self) -> list[dict[str, Any]]:
        raw = self.account["properties"].get("hyprialInvites")
        return json.loads(raw) if isinstance(raw, str) and raw else []

    def __enter__(self) -> "FakeCasdoor":
        self._thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
