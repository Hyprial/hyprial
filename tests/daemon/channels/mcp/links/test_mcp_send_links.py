"""WP6: the MCP harness surface carries summary, fan-out and body reads.

These tests pin the MCP boundary only: target wrapping, the mandatory
summary, the body-read tool and mutation flags. What the daemon decides about
a held or refused send is forwarded verbatim and never turned into a client
side error or a retry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from hyprial.daemon import HOLD_REASONS
from hyprial.daemon.impl.mcp import StatelessDaemonProxy, create_mcp_server
from hyprial.daemon.impl.mcp.api import DaemonRequestRejected
from hyprial.kernel import ipc_errors

pytestmark = pytest.mark.anyio

Responder = Callable[[str, dict[str, Any]], dict[str, Any]]


class _Connection:
    def __init__(self, calls: list[dict[str, Any]], responder: Responder) -> None:
        self._calls = calls
        self._responder = responder

    async def request(
        self, method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        self._calls.append(
            {"method": method, "params": params, "mutation": request_id is not None}
        )
        return self._responder(method, params)


class _Factory:
    def __init__(self, responder: Responder | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responder = responder or (lambda method, _params: {"ok": True})

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[_Connection]:
        yield _Connection(self.calls, self._responder)


@asynccontextmanager
async def _client(server: Any) -> AsyncIterator[ClientSession]:
    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        yield client


def _server(responder: Responder | None = None) -> tuple[Any, _Factory]:
    factory = _Factory(responder)
    server = create_mcp_server(
        StatelessDaemonProxy(factory), actor="alice", session_ref="session-1"
    )
    return server, factory


async def test_send_wraps_a_string_target_and_carries_summary() -> None:
    server, factory = _server()

    async with _client(server) as client:
        result = await client.call_tool(
            "harness_send", {"to": "agent:a", "summary": "s", "message": "hello"}
        )

    assert result.is_error is not True
    (call,) = factory.calls
    assert call["method"] == "message.send"
    assert call["params"]["to"] == ["agent:a"]
    assert call["params"]["summary"] == "s"
    assert call["params"]["message"] == "hello"
    assert call["mutation"] is True


async def test_send_passes_a_target_list_through_unchanged() -> None:
    server, factory = _server()

    async with _client(server) as client:
        result = await client.call_tool(
            "harness_send",
            {"to": ["agent:b", "agent:a"], "summary": "s", "message": "hello"},
        )

    assert result.is_error is not True
    assert factory.calls[0]["params"]["to"] == ["agent:b", "agent:a"]


@pytest.mark.parametrize("targets", [[], [""]])
async def test_an_invalid_target_list_is_a_tool_error_without_ipc(
    targets: list[str],
) -> None:
    server, factory = _server()

    async with _client(server) as client:
        result = await client.call_tool(
            "harness_send", {"to": targets, "summary": "s", "message": "hello"}
        )

    assert result.is_error is True
    assert factory.calls == []


async def test_a_missing_summary_is_a_tool_error_without_ipc() -> None:
    server, factory = _server()

    async with _client(server) as client:
        result = await client.call_tool(
            "harness_send", {"to": "agent:a", "message": "hello"}
        )

    assert result.is_error is True
    assert factory.calls == []


async def test_reply_carries_summary() -> None:
    server, factory = _server()

    async with _client(server) as client:
        result = await client.call_tool(
            "harness_reply", {"messageId": "m1", "summary": "s", "message": "done"}
        )

    assert result.is_error is not True
    (call,) = factory.calls
    assert call["method"] == "message.reply"
    assert call["params"]["summary"] == "s"
    assert call["mutation"] is True


async def test_read_body_is_registered_and_a_mutation() -> None:
    server, factory = _server()

    async with _client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        result = await client.call_tool("harness_read_body", {"messageId": "m1"})

    assert "harness_read_body" in names
    assert result.is_error is not True
    (call,) = factory.calls
    assert call["method"] == "message.read_body"
    assert call["params"]["messageId"] == "m1"
    assert call["mutation"] is True


async def test_read_passes_the_daemon_result_through() -> None:
    payload = {"ok": True, "messages": [{"summary": "s", "bodyBytes": 9}]}
    server, _ = _server(lambda _method, _params: payload)

    async with _client(server) as client:
        result = await client.call_tool("harness_read", {})

    assert result.is_error is not True
    assert result.structured_content == payload
    assert "payload" not in result.structured_content


async def test_an_unavailable_body_keeps_the_daemon_code() -> None:
    def responder(_method: str, _params: dict[str, Any]) -> dict[str, Any]:
        raise DaemonRequestRejected(ipc_errors.MESSAGE_BODY_UNAVAILABLE, "no")

    server, factory = _server(responder)

    async with _client(server) as client:
        result = await client.call_tool("harness_read_body", {"messageId": "m1"})

    assert result.is_error is True
    assert ipc_errors.MESSAGE_BODY_UNAVAILABLE in result.content[0].text
    assert len(factory.calls) == 1


async def test_a_refused_reply_is_a_tool_error_without_a_retry() -> None:
    def responder(_method: str, _params: dict[str, Any]) -> dict[str, Any]:
        raise DaemonRequestRejected(ipc_errors.LINK_RECLAIMED, "gone")

    server, factory = _server(responder)

    async with _client(server) as client:
        result = await client.call_tool(
            "harness_reply", {"messageId": "m1", "summary": "s", "message": "done"}
        )

    assert result.is_error is True
    assert ipc_errors.LINK_RECLAIMED in result.content[0].text
    assert len(factory.calls) == 1


@pytest.mark.parametrize(
    "target",
    [
        "user:owner",
        "user:someone-else",
        "agent:op:node-1:bob",
        "route:lark:ops",
    ],
)
async def test_send_keeps_the_daemon_verdict_for_every_target_kind(
    target: str,
) -> None:
    verdict = {
        "ok": True,
        "queued": True,
        "holdReason": ipc_errors.LINK_WINDOW_FULL,
        "deliveries": [
            {
                "target": target,
                "accepted": True,
                "queued": True,
                "holdReason": ipc_errors.LINK_WINDOW_FULL,
            }
        ],
    }
    server, factory = _server(lambda _method, _params: verdict)

    async with _client(server) as client:
        result = await client.call_tool(
            "harness_send", {"to": target, "summary": "s", "message": "hello"}
        )

    assert result.is_error is not True
    assert result.structured_content == verdict
    assert [call["method"] for call in factory.calls] == ["message.send"]
    assert factory.calls[0]["params"]["to"] == [target]


@pytest.mark.parametrize("reason", sorted(HOLD_REASONS))
async def test_a_hold_is_a_successful_single_call_result(reason: str) -> None:
    payload = {
        "ok": True,
        "queued": True,
        "holdReason": reason,
        "deliveries": [
            {"target": "agent:a", "accepted": True, "queued": True, "holdReason": reason}
        ],
    }
    server, factory = _server(lambda _method, _params: payload)

    async with _client(server) as client:
        result = await client.call_tool(
            "harness_send", {"to": "agent:a", "summary": "s", "message": "hello"}
        )

    assert result.is_error is not True
    assert result.structured_content == payload
    assert len(factory.calls) == 1


def test_the_worker_and_claude_allowlists_include_the_body_tool() -> None:
    from hyprial.daemon.impl.harnesses.turn_delivery.channel.worker_channel import (
        WORKER_HARNESS_TOOLS,
    )
    from hyprial.shell.impl.cli.commands.agent.runtime.claude import (
        _CLAUDE_HARNESS_TOOLS,
    )

    assert "harness_read_body" in WORKER_HARNESS_TOOLS
    assert "harness_read_body" in _CLAUDE_HARNESS_TOOLS
