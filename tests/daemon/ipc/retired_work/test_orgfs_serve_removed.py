"""The OrgFS resident-holder role is gone from every public surface.

Decision: orgfs-minimal-plan-2026-11-24 Q3 / A5 (owner 2026-11-25).  OrgFS no
longer reports which changes other nodes still lack and keeps no resident
holder role.  ``orgfs.serve`` leaves the request catalog, the migration record,
the IPC stats table, the OrgFS bridge and the MCP tool list; ``OrgFs`` and the
runtime lose ``serve``; the CLI loses ``hyprial org fs space serve``; directory
announcements stop advertising a ``resident`` role and post-rebuild catch-up
only schedules the deferred-peer half.  ``status``, ``checkout`` and the local
space purge are untouched.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession
from typer.testing import CliRunner

from hyprial.daemon.impl.application.messaging import orgfs_bridge
from hyprial.daemon.impl.ipc.router import _IPC_STATS_METHODS
from hyprial.daemon.impl.mcp import StatelessDaemonProxy, create_mcp_server
from hyprial.daemon.impl.orgfs.api import OrgFs
from hyprial.daemon.impl.orgfs.composition import vocabulary
from hyprial.daemon.impl.orgfs.composition.directory import RuntimeDirectory
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.request_catalog.migration import MIGRATED_METHODS
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.shell.impl.cli.commands.common.root import app as cli_root_app
from hyprial.shell.impl.cli.commands.fs.commands import space_app
from hyprial.shell.impl.cli.runtime import entry as _cli_entry

_REMOVED_METHOD = "orgfs.serve"


class _UnusedBridge:
    async def request(
        self, method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        raise AssertionError(f"listing tools must not call the daemon: {method}")


class _UnusedFactory:
    @asynccontextmanager
    async def connect(self) -> AsyncIterator[_UnusedBridge]:
        yield _UnusedBridge()


def test_request_catalog_and_migration_record_drop_serve() -> None:
    names = set(METHOD_REGISTRY.names)
    assert {"orgfs.checkout", "orgfs.status", "orgfs.space_purge"} <= names
    assert _REMOVED_METHOD not in names
    assert _REMOVED_METHOD not in MIGRATED_METHODS


def test_ipc_router_and_orgfs_bridge_tables_drop_serve() -> None:
    assert "orgfs.checkout" in orgfs_bridge._ORGFS_MUTATING_METHODS
    tables = (
        _IPC_STATS_METHODS,
        orgfs_bridge._ORGFS_MUTATING_METHODS,
        orgfs_bridge._ORGFS_READ_METHODS,
        orgfs_bridge._ORG_CONTEXT_READ_METHODS,
    )
    for table in tables:
        assert _REMOVED_METHOD not in table


def test_orgfs_protocol_and_runtime_drop_serve() -> None:
    assert hasattr(OrgFs, "checkout")
    assert hasattr(OrgFsRuntime, "checkout")
    assert not hasattr(OrgFs, "serve")
    assert not hasattr(OrgFsRuntime, "serve")
    assert not hasattr(vocabulary, "_ServeSpace")


@pytest.mark.anyio
async def test_mcp_tool_list_has_no_serve_tool() -> None:
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
    assert "orgfs_serve" not in names


def test_cli_space_group_has_no_serve_command() -> None:
    names = {command.name for command in space_app.registered_commands}
    assert {"status", "checkout"} <= names
    assert "serve" not in names
    assert _cli_entry is not None
    result = CliRunner().invoke(cli_root_app, ["org", "fs", "space", "serve", "--help"])
    assert result.exit_code != 0


def _directory_host(replicas: dict[str, object]) -> SimpleNamespace:
    space = SimpleNamespace(space_id="space-1")
    member = SimpleNamespace(user="user:me", mode="rw")
    return SimpleNamespace(
        author="user:me",
        node_id="node-me",
        facade=SimpleNamespace(
            spaces=lambda: (space,), members=lambda _space_id: (member,)
        ),
        _resource_lock=threading.RLock(),
        _pending_announces_lock=threading.RLock(),
        _deferred_supplier_online=set(),
        _holders={"space-1": {"node-peer"}},
        _replicas=replicas,
        _supplier_online=lambda _peer: True,
        _supplier_sync_effects=lambda _peer: [],
    )


def test_announcements_carry_no_resident_role() -> None:
    host = _directory_host({"space-1": object()})
    spaces = RuntimeDirectory._announcement_spaces(host)
    assert spaces == ({"spaceId": "space-1", "roles": ["member"], "mode": "rw"},)


def test_post_rebuild_catch_up_only_targets_deferred_peers() -> None:
    host = _directory_host({"space-1": object()})
    effects = RuntimeDirectory._post_rebuild_effects(host)
    assert [effect.action for effect in effects] == ["post-rebuild-log"]
    details = dict(effects[0].details)
    assert details == {"deferredPeers": 0, "syncTargets": 0}
