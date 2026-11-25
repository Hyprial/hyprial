"""OrgFS historical reads are gone from every daemon surface.

Decision: orgfs-minimal-plan-2026-11-24 A1.  ``journal_history``, ``read_at``
and ``stat_at`` have no product consumer outside the history panel, and the
minimal OrgFS keeps no per-commit journal to read them from.  The methods
``orgfs.read_at`` / ``orgfs.stat_at`` leave the request catalog, the migration
record, the IPC stats table, the OrgFS bridge and the MCP tool list; the
``OrgFs`` Protocol and ``LocalOrgFs`` lose the three attributes.  ``history``
left later in slice A3 (owner decision Q5); ``restore`` stays.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from hyprial.daemon.impl.application.messaging import orgfs_bridge
from hyprial.daemon.impl.ipc.router import _IPC_STATS_METHODS
from hyprial.daemon.impl.mcp import StatelessDaemonProxy, create_mcp_server
from hyprial.daemon.impl.orgfs.api import OrgFs
from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from hyprial.daemon.impl.request_catalog.migration import MIGRATED_METHODS
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY

_REMOVED_METHODS = ("orgfs.read_at", "orgfs.stat_at")
_REMOVED_TOOLS = ("orgfs_read_at", "orgfs_stat_at")
_REMOVED_ATTRIBUTES = ("journal_history", "read_at", "stat_at")


class _UnusedBridge:
    async def request(
        self, method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        raise AssertionError(f"listing tools must not call the daemon: {method}")


class _UnusedFactory:
    @asynccontextmanager
    async def connect(self) -> AsyncIterator[_UnusedBridge]:
        yield _UnusedBridge()


def test_request_catalog_and_migration_record_drop_history_reads() -> None:
    names = set(METHOD_REGISTRY.names)
    assert "orgfs.restore" in names
    for method in _REMOVED_METHODS:
        assert method not in names
        assert method not in MIGRATED_METHODS


def test_ipc_router_and_orgfs_bridge_tables_drop_history_reads() -> None:
    assert "orgfs.restore" in _IPC_STATS_METHODS
    tables = (
        _IPC_STATS_METHODS,
        orgfs_bridge._ORG_CONTEXT_READ_METHODS,
        orgfs_bridge._ORGFS_READ_METHODS,
    )
    for table in tables:
        for method in _REMOVED_METHODS:
            assert method not in table


def test_orgfs_protocol_and_local_facade_drop_history_reads() -> None:
    assert hasattr(OrgFs, "restore")
    assert hasattr(LocalOrgFs, "restore")
    for attribute in _REMOVED_ATTRIBUTES:
        assert not hasattr(OrgFs, attribute), attribute
        assert not hasattr(LocalOrgFs, attribute), attribute


@pytest.mark.anyio
async def test_mcp_tool_list_drops_history_reads() -> None:
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
    assert "orgfs_restore" in names
    for tool in _REMOVED_TOOLS:
        assert tool not in names
