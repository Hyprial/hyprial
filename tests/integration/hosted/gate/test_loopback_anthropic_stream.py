"""The synthetic Anthropic stream is one the real Claude worker can settle on.

Regression for the hosted-agent-e2e claude-headless scenario once its
readiness was fixed: the Agent SDK rejected ``message_start`` without
``model`` (KP-017, MessageParseError), and the CLI it bundles defers MCP tools
behind ``ToolSearch`` so ``harness_reply`` was never in ``tools`` and the
reply timed out.  These tests start no harness.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest


PROVIDER = Path(__file__).resolve().parents[4] / "contract/e2e/_fake_provider.py"
MESSAGE_ID = "54f581f7-a131-5b3b-bf02-1139753f950c"
REPLY_TOOL = "mcp__harness-bridge__harness_reply"


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Any:
    spec = importlib.util.spec_from_file_location("loopback_fake_provider", PROVIDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _events(chunks: Any) -> list[dict[str, Any]]:
    events = []
    for chunk in chunks:
        for line in chunk.decode().splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line.removeprefix("data: ")))
    return events


def _assert_complete_message(events: list[dict[str, Any]], stop_reason: str) -> None:
    start = events[0]
    assert start["type"] == "message_start"
    message = start["message"]
    assert message["type"] == "message"
    assert message["role"] == "assistant"
    assert isinstance(message["model"], str) and message["model"]
    assert set(message["usage"]) >= {"input_tokens", "output_tokens"}
    (delta,) = [event for event in events if event["type"] == "message_delta"]
    assert delta["delta"]["stop_reason"] == stop_reason
    assert events[-1]["type"] == "message_stop"


def test_text_stream_is_a_complete_message(provider: Any) -> None:
    events = _events(provider._anthropic_chunks("LOOPBACK_REPLY", terminate=True))

    _assert_complete_message(events, "end_turn")


def test_tool_stream_is_a_complete_message_with_unique_tool_use_ids(provider: Any) -> None:
    first = _events(provider._anthropic_tool_chunks("ToolSearch", {"query": "q"}, None))
    second = _events(provider._anthropic_tool_chunks(REPLY_TOOL, {"messageId": MESSAGE_ID}, None))

    _assert_complete_message(first, "tool_use")
    _assert_complete_message(second, "tool_use")
    ids = [
        event["content_block"]["id"]
        for event in (*first, *second)
        if event["type"] == "content_block_start"
    ]
    assert len(ids) == len(set(ids)) == 2


def _body(tools: list[str], *, called: tuple[str, ...] = (), message_id: bool = True) -> dict[str, Any]:
    prompt = f"messageId: {MESSAGE_ID}" if message_id else "no id here"
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": prompt}]},
        {"role": "user", "content": [{"type": "text", "text": f"deferred: {REPLY_TOOL}"}]},
    ]
    if called:
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": f"toolu_{n}", "name": name, "input": {}}
                    for n, name in enumerate(called)
                ],
            }
        )
    return {"tools": [{"name": name} for name in tools], "messages": messages}


def test_a_deferred_reply_tool_is_loaded_through_tool_search(provider: Any) -> None:
    call = provider._harness_reply_call(_body(["Bash", "ToolSearch"]), "LOOPBACK_REPLY")

    assert call == ("ToolSearch", {"query": f"select:{REPLY_TOOL}", "max_results": 5}, None)


def test_the_loaded_reply_tool_then_settles_the_request(provider: Any) -> None:
    call = provider._harness_reply_call(
        _body(["ToolSearch", REPLY_TOOL], called=("ToolSearch",)), "LOOPBACK_REPLY"
    )

    assert call is not None
    name, arguments, _namespace = call
    assert name == REPLY_TOOL
    assert arguments["messageId"] == MESSAGE_ID


@pytest.mark.parametrize(
    "body",
    [
        _body(["ToolSearch"], called=("ToolSearch",)),
        _body(["ToolSearch"], message_id=False),
        _body(["Bash"]),
        _body(["ToolSearch", REPLY_TOOL], called=("ToolSearch", REPLY_TOOL)),
    ],
    ids=["searched-once", "no-request", "no-tool-search", "already-replied"],
)
def test_tool_search_is_never_a_loop(provider: Any, body: dict[str, Any]) -> None:
    assert provider._harness_reply_call(body, "LOOPBACK_REPLY") is None
