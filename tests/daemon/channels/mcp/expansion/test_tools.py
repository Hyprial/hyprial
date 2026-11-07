"""MCP PAC expansion tool schema and identity binding."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from hyprial.daemon.impl.mcp import StatelessDaemonProxy, create_mcp_server


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(self, method: str, params: dict[str, Any], *, request_id=None):
        self.calls.append((method, params))
        return {"ok": True, "childGraphId": "child-1"}


class Factory:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    @asynccontextmanager
    async def connect(self):
        yield self.recorder


@pytest.mark.anyio
async def test_workflow_complete_optional_expansion_is_forwarded_and_plan_is_scoped() -> None:
    recorder = Recorder()
    server = create_mcp_server(
        StatelessDaemonProxy(Factory(recorder)),
        actor="agent:owner:node:planner",
        session_ref="session-1",
        workflow_tools=True,
    )
    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        complete = await client.call_tool(
            "workflow_complete",
            {
                "graphId": "parent",
                "nodeId": "plan",
                "requestId": "request-1",
                "reasonRef": "evidence",
                "expansion": "version: 2\nnodes: []\n",
            },
        )
        preview = await client.call_tool(
            "workflow_expansion_plan",
            {
                "graphId": "parent",
                "nodeId": "build",
                "yaml": "version: 2\nnodes: []\n",
            },
        )
    assert complete.is_error is not True
    assert preview.is_error is not True
    assert recorder.calls == [
        (
            "workflow.complete",
            {
                "graphId": "parent",
                "nodeId": "plan",
                "requestId": "request-1",
                "reasonRef": "evidence",
                "expansion": "version: 2\nnodes: []\n",
                "actor": "agent:owner:node:planner",
                "sessionRef": "session-1",
            },
        ),
        (
            "workflow.expansion.plan",
            {
                "graphId": "parent",
                "nodeId": "build",
                "yaml": "version: 2\nnodes: []\n",
                "actor": "agent:owner:node:planner",
                "sessionRef": "session-1",
            },
        ),
    ]


@pytest.mark.anyio
async def test_workflow_fail_does_not_advertise_expansion() -> None:
    recorder = Recorder()
    server = create_mcp_server(
        StatelessDaemonProxy(Factory(recorder)),
        actor="agent:owner:node:planner",
        session_ref="session-1",
        workflow_tools=True,
    )
    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert "expansion" not in str(tools["workflow_fail"].input_schema)


@pytest.mark.anyio
async def test_expansion_strings_have_the_frozen_transport_bound() -> None:
    recorder = Recorder()
    server = create_mcp_server(
        StatelessDaemonProxy(Factory(recorder)),
        actor="agent:owner:node:planner",
        session_ref="session-1",
        workflow_tools=True,
    )
    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    complete_schema = str(tools["workflow_complete"].input_schema)
    plan_schema = str(tools["workflow_expansion_plan"].input_schema)
    assert "65536" in complete_schema
    assert "65536" in plan_schema


@pytest.mark.anyio
async def test_interactive_server_does_not_advertise_expansion_tools() -> None:
    recorder = Recorder()
    server = create_mcp_server(
        StatelessDaemonProxy(Factory(recorder)),
        actor="agent:owner:node:interactive",
        session_ref="session-1",
    )
    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert "workflow_expansion_plan" not in names
    assert "workflow_complete" not in names


@pytest.mark.anyio
async def test_mcp_rejects_expansion_over_utf8_byte_bound_before_ipc() -> None:
    recorder = Recorder()
    server = create_mcp_server(
        StatelessDaemonProxy(Factory(recorder)),
        actor="agent:owner:node:planner",
        session_ref="session-1",
        workflow_tools=True,
    )
    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        result = await client.call_tool(
            "workflow_expansion_plan",
            {
                "graphId": "parent",
                "nodeId": "build",
                "yaml": "中" * 21_846,
            },
        )
    assert result.is_error is True
    assert recorder.calls == []
