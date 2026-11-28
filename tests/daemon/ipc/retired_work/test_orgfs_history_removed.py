"""OrgFS node history is gone from every daemon and CLI surface.

Decision: orgfs-minimal-plan-2026-11-24 Q5 (owner 2026-11-25), slice A3.
The minimal OrgFS keeps no history.  ``orgfs.history`` leaves the request
catalog, the migration record, the IPC stats table, the OrgFS bridge and the
MCP tool list; the ``OrgFs`` Protocol and ``LocalOrgFs`` lose ``history`` and
the value object ``HistoryEntry`` is deleted; ``hyprial org fs history`` is no
longer a command.  ``restore`` and ``trash`` stay unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession
from typer.testing import CliRunner

from hyprial.daemon.impl import orgfs
from hyprial.daemon.impl.application.messaging import orgfs_bridge
from hyprial.daemon.impl.ipc.router import _IPC_STATS_METHODS
from hyprial.daemon.impl.mcp import StatelessDaemonProxy, create_mcp_server
from hyprial.daemon.impl.orgfs import api as orgfs_api
from hyprial.daemon.impl.orgfs.api import OrgFs
from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from hyprial.daemon.impl.request_catalog.migration import MIGRATED_METHODS
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.shell.impl.cli.commands.fs.commands import fs_app

_KEPT_METHODS = ("orgfs.restore", "orgfs.trash")


class _UnusedBridge:
    async def request(
        self, method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        raise AssertionError(f"listing tools must not call the daemon: {method}")


class _UnusedFactory:
    @asynccontextmanager
    async def connect(self) -> AsyncIterator[_UnusedBridge]:
        yield _UnusedBridge()


def test_request_catalog_and_migration_record_drop_history() -> None:
    names = set(METHOD_REGISTRY.names)
    for method in _KEPT_METHODS:
        assert method in names
    assert "orgfs.history" not in names
    assert "orgfs.history" not in MIGRATED_METHODS


def test_ipc_router_and_orgfs_bridge_tables_drop_history() -> None:
    for method in _KEPT_METHODS:
        assert method in _IPC_STATS_METHODS
    tables = (
        _IPC_STATS_METHODS,
        orgfs_bridge._ORG_CONTEXT_READ_METHODS,
        orgfs_bridge._ORGFS_READ_METHODS,
    )
    for table in tables:
        assert "orgfs.history" not in table


def test_orgfs_protocol_local_facade_and_value_object_drop_history() -> None:
    for attribute in ("restore", "trash"):
        assert hasattr(OrgFs, attribute)
        assert hasattr(LocalOrgFs, attribute)
    assert not hasattr(OrgFs, "history")
    assert not hasattr(LocalOrgFs, "history")
    assert not hasattr(orgfs_api, "HistoryEntry")
    assert not hasattr(orgfs, "HistoryEntry")


def test_cli_drops_fs_history() -> None:
    names = {command.name for command in fs_app.registered_commands}
    assert {"rm", "watch"} <= names
    assert "history" not in names
    result = CliRunner().invoke(fs_app, ["history", "space-1", "id:node-1"])
    assert result.exit_code != 0


@pytest.mark.anyio
async def test_mcp_tool_list_drops_history() -> None:
    server = create_mcp_server(
        StatelessDaemonProxy(_UnusedFactory()),
        actor="agent:owner:node:worker",
        session_ref="session-fixed",
    )
    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert {"orgfs_restore", "orgfs_trash"} <= names
    assert "orgfs_history" not in names
