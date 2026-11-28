"""OrgFS content purge is gone from every public surface.

Decision: orgfs-minimal-plan-2026-11-24 Q4 and orgfs-minimal-design-2026-11-25
§1.1–§1.2, phase D package D4a.  Content purge only removes current state; no
network-wide history purge and no hash ban.  ``orgfs.purge_plan``,
``orgfs.purge``, ``orgfs.purge_status`` and ``orgfs.unban`` leave the request
catalog, the migration record, the IPC stats table, the OrgFS bridge and the
MCP tool list; ``OrgFs`` loses those four methods plus ``parked_commits`` and
``document_covers_member_add``; the package stops exporting the purge value
objects; the CLI loses ``hyprial org fs purge plan|status|unban`` and the
PLAN_ID argument of ``purge run``.  The local space purge (``orgfs.space_purge``
behind ``hyprial org fs purge run SPACE``) is untouched.  The old host classes
keep their implementations until package S deletes them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession
from typer.testing import CliRunner

import hyprial.daemon.impl.orgfs as orgfs_package
from hyprial.daemon.impl.application.messaging import orgfs_bridge
from hyprial.daemon.impl.ipc.router import _IPC_STATS_METHODS
from hyprial.daemon.impl.mcp import StatelessDaemonProxy, create_mcp_server
from hyprial.daemon.impl.orgfs import api as orgfs_api
from hyprial.daemon.impl.orgfs.api import OrgFs
from hyprial.daemon.impl.request_catalog.migration import MIGRATED_METHODS
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.shell.impl.cli.commands.common.root import app as cli_root_app
from hyprial.shell.impl.cli.commands.fs.commands import purge_app
from hyprial.shell.impl.cli.runtime import entry as _cli_entry

_REMOVED_METHODS = (
    "orgfs.purge_plan",
    "orgfs.purge",
    "orgfs.purge_status",
    "orgfs.unban",
)
_REMOVED_PROTOCOL_METHODS = (
    "purge_plan",
    "purge",
    "purge_status",
    "unban",
    "parked_commits",
    "document_covers_member_add",
)


class _UnusedBridge:
    async def request(
        self, method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        raise AssertionError(f"listing tools must not call the daemon: {method}")


class _UnusedFactory:
    @asynccontextmanager
    async def connect(self) -> AsyncIterator[_UnusedBridge]:
        yield _UnusedBridge()


def test_request_catalog_and_migration_record_drop_content_purge() -> None:
    names = set(METHOD_REGISTRY.names)
    assert {"orgfs.space_purge", "orgfs.status", "orgfs.remove"} <= names
    assert "orgfs.space_purge" in MIGRATED_METHODS
    for method in _REMOVED_METHODS:
        assert method not in names
        assert method not in MIGRATED_METHODS


def test_ipc_router_and_orgfs_bridge_tables_drop_content_purge() -> None:
    assert "orgfs.space_purge" in orgfs_bridge._ORGFS_MUTATING_METHODS
    assert "orgfs.space_purge" in _IPC_STATS_METHODS
    tables = (
        _IPC_STATS_METHODS,
        orgfs_bridge._ORGFS_MUTATING_METHODS,
        orgfs_bridge._ORGFS_READ_METHODS,
        orgfs_bridge._ORG_CONTEXT_READ_METHODS,
    )
    for table in tables:
        for method in _REMOVED_METHODS:
            assert method not in table


def test_orgfs_protocol_drops_content_purge_and_journal_queries() -> None:
    for kept in ("status", "remove", "document_authors", "checkout"):
        assert hasattr(OrgFs, kept)
    for removed in _REMOVED_PROTOCOL_METHODS:
        assert not hasattr(OrgFs, removed), removed
    assert not hasattr(orgfs_api, "PurgeTarget")


def test_orgfs_package_stops_exporting_purge_value_objects() -> None:
    assert "SpaceStatus" in orgfs_package.__all__
    for name in ("PurgePlan", "PurgeResult", "PurgeStatus", "PurgeTarget"):
        assert name not in orgfs_package.__all__
        assert not hasattr(orgfs_package, name), name
        assert not hasattr(orgfs_api, name), name


@pytest.mark.anyio
async def test_mcp_tool_list_has_no_content_purge_tool() -> None:
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
    assert "orgfs_status" in names
    for tool in ("orgfs_purge_plan", "orgfs_purge", "orgfs_purge_status", "orgfs_unban"):
        assert tool not in names


def test_cli_purge_group_keeps_only_the_local_space_purge() -> None:
    names = {command.name for command in purge_app.registered_commands}
    assert names == {"run"}
    assert _cli_entry is not None
    runner = CliRunner()
    for removed in ("plan", "status", "unban"):
        result = runner.invoke(cli_root_app, ["org", "fs", "purge", removed, "--help"])
        assert result.exit_code != 0, removed
    help_result = runner.invoke(cli_root_app, ["org", "fs", "purge", "run", "--help"])
    assert help_result.exit_code == 0
    assert "PLAN_ID" not in help_result.output
    assert "--yes" in help_result.output
