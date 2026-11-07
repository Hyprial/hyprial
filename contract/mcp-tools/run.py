#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from hyprial.daemon.impl.mcp import StatelessDaemonProxy, create_mcp_server  # noqa: E402


BATCH_RESPONSE = {
    "ok": True,
    "schemaVersion": 1,
    "format": "inbox-batch-v1",
    "cursor": "contract-cursor",
    "daemonEpoch": "contract-epoch",
    "actor": "contract-actor",
    "itemCount": 1,
    "totalBodyBytes": 9,
    "inlineLimitBytes": 16384,
    "items": [
        {
            "messageId": "m3",
            "deliveryId": "m3",
            "conversationId": "conversation-1",
            "from": "user:owner",
            "to": "contract-actor",
            "intent": "request",
            "kind": "message",
            "createdAtMs": 1790000000000,
            "summary": "read this",
            "body": "full body",
            "bodyBytes": 9,
            "origin": {"via": "operator"},
            "class": "human",
            "expectedAction": "reply",
        }
    ],
    "delivery": {
        "mode": "inline",
        "text": "[Harness Network inbox batch cursor=contract-cursor items=1]",
        "spillPath": None,
        "spillBytes": None,
        "spillSha256": None,
    },
}


class Connection:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def request(
        self, method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(method)
        if method == "message.pending.batch":
            return BATCH_RESPONSE
        return {"ok": True, "method": method, "actor": params["actor"]}


class Factory:
    def __init__(self) -> None:
        self.calls: list[str] = []

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Connection]:
        yield Connection(self.calls)


async def main() -> None:
    factory = Factory()
    server = create_mcp_server(
        StatelessDaemonProxy(factory),
        actor="contract-actor",
        session_ref="contract-session",
    )
    calls = [
        ("harness_send", {"to": "agent:bob", "summary": "hello", "message": "hello"}),
        ("harness_reply", {"messageId": "m1", "summary": "reply", "message": "reply"}),
        ("harness_read", {}),
        ("harness_read_body", {"messageId": "m3"}),
        ("harness_ack", {"messageId": "m2"}),
        ("harness_progress", {"deliveryId": "d1", "sinceSeq": 2}),
        ("harness_whoami", {}),
        ("harness_targets", {}),
        ("orgfs_spaces", {}),
        ("orgfs_create", {"name": "shared"}),
        ("orgfs_resolve", {"spaceId": "space-1", "path": "/docs"}),
        ("orgfs_ls", {"spaceId": "space-1", "path": "/"}),
        ("orgfs_stat", {"spaceId": "space-1", "node": "id:node-1"}),
        ("orgfs_read", {"spaceId": "space-1", "node": "id:node-1"}),
        (
            "orgfs_export",
            {
                "spaceId": "space-1",
                "node": "id:node-1",
                "destination": "/tmp/export.txt",
            },
        ),
        (
            "orgfs_import",
            {
                "spaceId": "space-1",
                "node": "id:node-1",
                "source": "/tmp/import.txt",
            },
        ),
        (
            "orgfs_write",
            {
                "spaceId": "space-1",
                "node": "id:node-1",
                "text": "hello",
                "baseVersion": "version-1",
                "expectVersion": "version-2",
            },
        ),
        ("orgfs_mkdir", {"spaceId": "space-1", "path": "/docs"}),
        (
            "orgfs_move",
            {
                "spaceId": "space-1",
                "sourcePath": "/docs",
                "destinationPath": "/archive",
            },
        ),
        ("orgfs_remove", {"spaceId": "space-1", "node": "id:node-1"}),
        (
            "orgfs_history",
            {
                "spaceId": "space-1",
                "node": "id:node-1",
                "limit": 25,
                "before": "version-3",
            },
        ),
        (
            "orgfs_read_at",
            {
                "spaceId": "space-1",
                "node": "id:node-1",
                "version": "version-1",
            },
        ),
        (
            "orgfs_stat_at",
            {
                "spaceId": "space-1",
                "node": "id:node-1",
                "version": "version-1",
            },
        ),
        ("orgfs_trash", {"spaceId": "space-1", "limit": 25}),
        (
            "orgfs_restore",
            {
                "spaceId": "space-1",
                "node": "id:node-1",
                "version": "version-1",
                "recursive": False,
            },
        ),
        (
            "orgfs_watch",
            {
                "spaceId": "space-1",
                "glob": "docs/**",
                "sinceVersion": "version-1",
            },
        ),
        ("orgfs_status", {"spaceId": "space-1"}),
        ("orgfs_purge_status", {"spaceId": "space-1", "planId": "plan-1"}),
        (
            "orgfs_invite",
            {"spaceId": "space-1", "user": "user:bob", "mode": "ro"},
        ),
        (
            "orgfs_remove_member",
            {"spaceId": "space-1", "user": "user:bob"},
        ),
        ("orgfs_members", {"spaceId": "space-1"}),
        ("orgfs_join", {"spaceId": "space-1"}),
    ]
    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        tools = {item.name for item in (await client.list_tools()).tools}
        assert tools == {name for name, _ in calls}
        results: dict[str, dict[str, Any] | None] = {}
        for name, arguments in calls:
            result = await client.call_tool(name, arguments)
            assert result.is_error is not True
            results[name] = result.structured_content
    assert results["harness_read"] == BATCH_RESPONSE
    assert set(results["harness_read"] or {}) == {
        "ok",
        "schemaVersion",
        "format",
        "cursor",
        "daemonEpoch",
        "actor",
        "itemCount",
        "totalBodyBytes",
        "inlineLimitBytes",
        "items",
        "delivery",
    }
    assert factory.calls == [
        "message.send",
        "message.reply",
        "message.pending.batch",
        "message.read_body",
        "message.ack",
        "progress.list",
        "identity.whoami",
        "targets",
        "orgfs.spaces",
        "orgfs.create",
        "orgfs.resolve",
        "orgfs.ls",
        "orgfs.stat",
        "orgfs.read",
        "orgfs.export",
        "orgfs.import",
        "orgfs.write",
        "orgfs.mkdir",
        "orgfs.move",
        "orgfs.remove",
        "orgfs.history",
        "orgfs.read_at",
        "orgfs.stat_at",
        "orgfs.trash",
        "orgfs.restore",
        "orgfs.watch",
        "orgfs.status",
        "orgfs.purge_status",
        "orgfs.invite",
        "orgfs.remove_member",
        "orgfs.members",
        "orgfs.join",
    ]
    print("mcp-tools: PASS 32/32")


if __name__ == "__main__":
    asyncio.run(main())
