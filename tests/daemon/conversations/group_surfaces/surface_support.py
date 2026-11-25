"""Harness for the group surfaces: the request HTTP host and the agent tools.

``http_server`` is the request HTTP server of ``test_group_wiring``: the
production catalog view, every HTTP-exposed route available, and each call
forwarded to ``app.handle`` of the group daemon with the IPC origin, so the
listener's schema check, identity injection and envelope run for real.

``agent_tools`` is the MCP harness server an agent's channel serves, with
the agent's fixed ``(actor, sessionRef)``; its proxy forwards each tool call
to ``app.handle`` of the same daemon, so the daemon's session fence and the
group membership check decide the answer.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from hyprial import __version__
from hyprial.daemon import daemon_catalog as local_catalog
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.mcp import StatelessDaemonProxy, create_mcp_server
from hyprial.daemon.impl.mcp.api import DaemonRequestRejected
from hyprial.shell.impl.request_http.catalog import build_view
from hyprial.shell.impl.request_http.config import Budgets, RequestHttpConfig
from hyprial.shell.impl.request_http.server import RequestHttpServer
from tests.daemon.conversations.group_requests.group_request_support import GroupDaemon
from tests.support.golden.app import OWNER as OWNER_NAME, SESSIONS

OWNER_ADDRESS = f"user:{OWNER_NAME}"


def http_server(booted: GroupDaemon) -> RequestHttpServer:
    config = RequestHttpConfig(
        socket_path=Path("/tmp/daemon.sock"), owner=OWNER_NAME, actor=OWNER_ADDRESS,
        host="127.0.0.1", port=0, allowed_origins=(), allowed_hosts=("127.0.0.1",),
        budgets=Budgets(15.0, 5.0, 30.0, 262144, 8388608, 8, 5.0),
        runtime_dir=Path("/tmp/request-http-runtime"), source=Path("/tmp/config.json"),
    )

    def request_once(method: str, params: dict[str, Any], **_kwargs: Any) -> Any:
        return booted.app.handle(method, dict(params), _trusted_message_origin="ipc")

    entries = []
    for entry in local_catalog()["methods"]:
        item = dict(entry)
        item.update(declared=True, reason=None,
                    state="available" if entry["httpExposed"] else "disabled")
        item["callable"] = item["state"] == "available"
        entries.append(item)
    server = RequestHttpServer(config, request_once=request_once)
    view = build_view(
        {"version": __version__, "nodeId": "node", "owner": OWNER_NAME,
         "epoch": "epoch-1", "phase": "serving"},
        {"schemaVersion": 1, "catalogVersion": "1", "epoch": "epoch-1",
         "packageVersion": __version__, "methods": entries},
        socket_path="/tmp/daemon.sock", local_entries=local_catalog()["methods"],
    )
    server._view = replace(view, routes=dict(view.routes))
    server._startup_declaration = view.declaration_digest
    server._startup_http = view.http_contract_digest
    server._snapshot_at = time.monotonic()
    return server


def http_call(server: RequestHttpServer, method: str, params: dict[str, Any],
              request_id: str = "req-1") -> tuple[int, dict[str, Any]]:
    """One HTTP request body through the listener: its status and envelope."""

    envelope = server._invoke(method, request_id, time.monotonic(), dict(params), None)
    return envelope.status, envelope.payload


class _Connection:
    def __init__(self, booted: GroupDaemon) -> None:
        self._booted = booted

    async def request(self, method: str, params: dict[str, Any], *,
                      request_id: str | None = None) -> dict[str, Any]:
        try:
            return self._booted.app.handle(method, dict(params), _trusted_message_origin="ipc")
        except DaemonRequestError as error:
            raise DaemonRequestRejected(error.code, str(error), error.data) from None


class _Factory:
    def __init__(self, booted: GroupDaemon) -> None:
        self._booted = booted

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[_Connection]:
        yield _Connection(self._booted)


@asynccontextmanager
async def agent_tools(booted: GroupDaemon, who: str) -> AsyncIterator[ClientSession]:
    """An MCP client of the harness tools served for agent *who*'s session."""

    server = create_mcp_server(
        StatelessDaemonProxy(_Factory(booted)),
        actor=booted.address(who), session_ref=SESSIONS[who],
    )
    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        yield client


__all__ = ["OWNER_ADDRESS", "agent_tools", "http_call", "http_server"]
