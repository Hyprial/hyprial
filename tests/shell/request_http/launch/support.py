"""Shared helpers of the listener-free server suites.

Decision: embedded-request-http-listen-false-20261119, B1 of 2822.  The daemon
side is the rebind suite's ``PhaseDaemon`` and ``Gate``; the in-process client is
``httpx.ASGITransport`` with base URL ``http://127.0.0.1`` (the Host header is the
only address check); the listener comparison goes through a real socket.
"""

from __future__ import annotations

import asyncio
import http.client
import json
from pathlib import Path
from typing import Any
import urllib.parse

import httpx

from hyprial.shell.impl.request_http.server import RequestHttpServer
from tests.shell.request_http.rebind.support import OWNER, Gate, PhaseDaemon, gui_config, write_home

ACTOR = f"user:{OWNER}"
BASE = "http://127.0.0.1"
CALLS: list[tuple[str, str, Any]] = [
    ("GET", "/readyz", None),
    ("GET", "/daemon/request", None),
    ("POST", "/daemon/request/conversations.list", {}),
]


def daemon_pair(root: Path) -> tuple[PhaseDaemon, Gate]:
    root.mkdir(parents=True, exist_ok=True)
    write_home(root, owner=OWNER)
    daemon = PhaseDaemon(root / "home" / "state" / "daemon.sock", phase="serving", owner=OWNER)
    daemon.handlers["conversations.list"] = lambda p: {
        "ok": True,
        "participant": p["from"],
        "conversations": [],
        "nextCursor": None,
        "recordAvailable": True,
    }
    return daemon, Gate(daemon, "serving")


def tree(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")}


async def acall(
    app: Any, method: str, path: str, token: str | None, body: Any = None, *, base: str = BASE, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, Any]]:
    sent = dict(headers or {})
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        response = await client.request(method, base + path, headers=sent, json=body)
    return response.status_code, response.json()


def status_of(app: Any, method: str, path: str, token: str | None, body: Any = None, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    status, payload = asyncio.run(acall(app, method, path, token, body, **kwargs))
    payload.pop("requestId", None)
    return status, payload


_OWN = object()


def over_socket(server: RequestHttpServer, method: str, path: str, body: Any = None, token: Any = _OWN) -> tuple[int, dict[str, Any]]:
    endpoint = urllib.parse.urlsplit(server.endpoint)
    connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=30)
    bearer = server.token if token is _OWN else token
    headers = {"Content-Type": "application/json"}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        connection.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=headers)
        response = connection.getresponse()
        parsed = json.loads(response.read())
    finally:
        connection.close()
    parsed.pop("requestId", None)
    return response.status, parsed


def embedded(root: Path, **kwargs: Any) -> tuple[RequestHttpServer, PhaseDaemon, Gate]:
    daemon, gate = daemon_pair(root)
    server = RequestHttpServer(gui_config(root, owner=OWNER), request_once=gate, listen=False, **kwargs)
    return server, daemon, gate
