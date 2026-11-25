"""A member agent reads its group's history with ``harness_read`` (slice GH).

The agent calls the harness tool it already has, ``harness_read``, with a
``conversationId`` (and the history page parameters); the MCP server signs
the call with the agent's fixed ``(actor, sessionRef)`` and the daemon
answers ``conversations.history`` under that verified session, so the group
membership check decides: a member reads every page, a non-member and a
removed member are refused.  Without ``conversationId`` the tool is the
inbox batch read it always was.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.daemon.conversations.group_delivery.delivery_support import batch
from tests.daemon.conversations.group_messages.group_message_support import (
    handle,
    history,
    make_group,
    send,
)
from tests.daemon.conversations.group_requests.group_request_support import (
    GroupDaemon,
    group_daemon,
)
from tests.daemon.conversations.group_surfaces.surface_support import agent_tools
from tests.support.golden.app import SESSIONS, golden_daemon as golden_daemon

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _as_agent(booted: GroupDaemon, who: str, group: str, **params: Any) -> dict[str, Any]:
    return booted.app.handle(
        "conversations.history",
        {"from": booted.address(who), "sessionRef": SESSIONS[who], "conversationId": group,
         **params},
        _trusted_message_origin="ipc",
    )


def _talk(booted: GroupDaemon, group: str) -> None:
    for index in range(3):
        booted.advance()
        send(booted, group, f"step {index}", key=f"agent-history-{index}")


async def _read(booted: GroupDaemon, who: str, **arguments: Any) -> Any:
    async with agent_tools(booted, who) as client:
        return await client.call_tool("harness_read", arguments)


async def test_a_member_agent_pages_the_group_history(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta"))
    _talk(booted, group)

    newest = await _read(booted, "alpha", conversationId=group, mode="tail", limit=2)
    assert newest.is_error is not True, newest.content
    older = await _read(booted, "alpha", conversationId=group, mode="older", limit=2,
                        cursor=newest.structured_content["nextOlderCursor"])
    assert older.is_error is not True, older.content

    assert newest.structured_content == _as_agent(booted, "alpha", group, mode="tail", limit=2)
    assert [entry["message"] for entry in newest.structured_content["entries"]] == [
        "step 1", "step 2",
    ]
    full = history(booted, group, limit=200)["entries"]
    assert [entry["messageId"] for entry in older.structured_content["entries"]] == [
        entry["messageId"] for entry in full[2:4]
    ]
    everything = await _read(booted, "beta", conversationId=group, limit=200)
    assert everything.structured_content["entries"] == full


def _refusal(result: Any) -> str:
    assert result.is_error is True
    return " ".join(getattr(item, "text", "") for item in result.content)


async def test_a_non_member_agent_is_refused(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha",))
    _talk(booted, group)

    result = await _read(booted, "gamma", conversationId=group)

    assert "CONVERSATION_NOT_MEMBER" in _refusal(result)


async def test_a_removed_member_agent_is_refused(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta"))
    _talk(booted, group)
    assert (await _read(booted, "beta", conversationId=group)).is_error is not True
    booted.advance()
    handle(booted, "conversations.members.remove", conversationId=group,
           memberId=booted.agent_id("beta"), idempotencyKey="agent-history-remove")

    result = await _read(booted, "beta", conversationId=group)

    assert "CONVERSATION_NOT_MEMBER" in _refusal(result)


async def test_without_a_conversation_the_tool_reads_the_inbox(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha",))
    send(booted, group, "please cut the release", key="agent-history-inbox",
         mentions=[booted.agent_id("alpha")])

    result = await _read(booted, "alpha")

    assert result.is_error is not True, result.content
    assert result.structured_content == batch(booted, "alpha")


async def test_paging_without_a_conversation_is_a_tool_error(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)

    result = await _read(booted, "alpha", limit=5)

    assert "conversationId" in _refusal(result)


def test_the_group_line_names_the_tool(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha",))
    send(booted, group, "please cut the release", key="agent-history-line",
         mentions=[booted.agent_id("alpha")])

    text = batch(booted, "alpha")["delivery"]["text"]

    line = next(line for line in text.splitlines() if line.startswith("group: "))
    assert f"harness_read with conversationId {group}" in line
