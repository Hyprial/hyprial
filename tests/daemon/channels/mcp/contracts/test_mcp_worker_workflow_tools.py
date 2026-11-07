"""A managed worker with no shell can complete or fail its own PAC node.

org-improver, 2026-09-24: every claude-tier (Agent SDK) node stayed
``requested`` until its deadline. The worker is pre-authorized for the
harness-bridge MCP tools only (``strictMcpConfig`` + ``allowedTools``), and
``hyprial workflow complete`` needs Bash, which a headless worker has no way
to get approved. The worker's MCP server now carries ``workflow_complete`` /
``workflow_fail``; they sign with the worker's fixed ``(actor, sessionRef)``
exactly as the CLI does, so the daemon's owner fence is what decides.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.harnesses.worker_channel import WORKER_HARNESS_TOOLS, build_worker_channel
from hyprial.daemon.impl.mcp import StatelessDaemonProxy, create_mcp_server
from tests.daemon.channels.mcp.contracts.test_mcp_delegate_removed import make_daemon

WORKER = "agent:op:node-1:pac-worker"
SESSION = "worker-session-1"
ARGS = {
    "graphId": "wf-1",
    "nodeId": "plan",
    "requestId": "req-1",
    "reasonRef": "spec/plan@abc123",
    "outputText": "inline verdict",
}


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(
        self, method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        return {"ok": True}


class _Factory:
    def __init__(self, target: Any) -> None:
        self._target = target

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Any]:
        yield self._target


async def _tool_names(server) -> set[str]:  # type: ignore[no-untyped-def]
    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        return {tool.name for tool in (await client.list_tools()).tools}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "method"),
    [("workflow_complete", "workflow.complete"), ("workflow_fail", "workflow.fail")],
)
async def test_worker_tool_sends_the_workers_own_identity(tool: str, method: str) -> None:
    recorder = _Recorder()
    server = create_mcp_server(
        StatelessDaemonProxy(_Factory(recorder)),
        actor=WORKER,
        session_ref=SESSION,
        workflow_tools=True,
    )
    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        result = await client.call_tool(tool, ARGS)

    assert result.is_error is not True
    # The same request shape `hyprial workflow complete` sends from a worker:
    # the node coordinates plus the daemon-bound session identity, nothing
    # the caller could use to claim someone else's node.
    assert recorder.calls == [(method, {**ARGS, "actor": WORKER, "sessionRef": SESSION})]


@pytest.mark.anyio
async def test_only_the_worker_server_advertises_workflow_tools() -> None:
    proxy = StatelessDaemonProxy(_Factory(_Recorder()))
    worker = await _tool_names(
        create_mcp_server(proxy, actor=WORKER, session_ref=SESSION, workflow_tools=True)
    )
    interactive = await _tool_names(
        create_mcp_server(proxy, actor=WORKER, session_ref=SESSION)
    )

    assert {"workflow_complete", "workflow_fail"} <= worker
    assert not ({"workflow_complete", "workflow_fail"} & interactive)


@pytest.mark.anyio
async def test_worker_tools_route_to_registered_daemon_methods(tmp_path: Path) -> None:
    """Against the real dispatch: the tools must reach a registered method
    (here refused as WORKFLOW_UNAVAILABLE -- no service in this daemon), never
    METHOD_NOT_FOUND, the failure shape harness_delegate once shipped with."""

    app = make_daemon(tmp_path)
    codes: list[tuple[str, str | None]] = []

    class _Bridge:
        async def request(
            self, method: str, params: dict[str, Any], *, request_id: str | None = None
        ) -> dict[str, Any]:
            try:
                result = app.handle(method, params)
            except DaemonRequestError as error:
                codes.append((method, error.code))
                raise
            codes.append((method, None))
            return result

    server = create_mcp_server(
        StatelessDaemonProxy(_Factory(_Bridge())),
        actor=WORKER,
        session_ref=SESSION,
        workflow_tools=True,
    )
    try:
        async with (
            InMemoryTransport(server) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as client,
        ):
            await client.initialize()
            await client.call_tool("workflow_complete", ARGS)
            await client.call_tool("workflow_fail", ARGS)
    finally:
        app._close()

    assert [method for method, _code in codes] == ["workflow.complete", "workflow.fail"]
    assert all(code != "METHOD_NOT_FOUND" for _method, code in codes)


def test_a_managed_worker_is_pre_authorized_for_the_workflow_tools(tmp_path: Path) -> None:
    assert {"workflow_complete", "workflow_fail"} <= set(WORKER_HARNESS_TOOLS)
    channel = build_worker_channel(
        actor=WORKER,
        session_ref=SESSION,
        hyprial_home=tmp_path / "home",
        state_dir=tmp_path / "state",
    )
    assert "mcp__harness-bridge__workflow_complete" in channel.allowed_tools
    assert "mcp__harness-bridge__workflow_fail" in channel.allowed_tools


@pytest.mark.anyio
async def test_the_worker_stdio_server_is_the_one_with_workflow_tools(monkeypatch) -> None:
    """Assembly point: the module-private `hyprial.daemon.impl.mcp.entry
    agent-channel` entry runs serve_worker_stdio, so
    that is where the tools must be switched on -- not only in a test's own
    create_mcp_server call."""

    import hyprial.daemon.impl.mcp.server as server_module

    built: list[dict[str, Any]] = []
    real = server_module.create_mcp_server

    class _NoStdio:
        def __init__(self, inner) -> None:  # type: ignore[no-untyped-def]
            self.inner = inner

        async def run_stdio_async(self) -> None:
            return None

    def capture(proxy, **kwargs):  # type: ignore[no-untyped-def]
        built.append(kwargs)
        return _NoStdio(real(proxy, **kwargs))

    monkeypatch.setattr(server_module, "create_mcp_server", capture)
    await server_module.serve_worker_stdio(
        StatelessDaemonProxy(_Factory(_Recorder())), actor=WORKER, session_ref=SESSION
    )

    assert len(built) == 1
    assert built[0].get("workflow_tools") is True
    assert {"workflow_complete", "workflow_fail"} <= await _tool_names(
        real(StatelessDaemonProxy(_Factory(_Recorder())), **built[0])
    )
