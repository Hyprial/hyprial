"""The scripted Messages mock that lets the dsh-tui E2E call Harness tools.

The dsh-tui acceptance needs a provider that answers a wake with a
``harness_read`` tool call and then a ``harness_reply`` for the pending
message, without any real key. These cases pin the decision table and the
stream shape the native ``dsh-llm-deepseek`` adapter validates (a usage object
in ``message_start``, closed blocks, a stop reason before ``message_stop``);
the text-only ``_fake_provider`` omits both and dsh rejects it.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "contract" / "e2e"))

from _messages_tool_mock import (  # noqa: E402
    HARNESS_READ,
    HARNESS_REPLY,
    TOOLS_MISSING,
    ScriptedMessagesMock,
    decide,
    messages_sse,
)

MARKER = r"DSH-E2E-[0-9a-f]+"
WAKE = "[Harness Network] Harness backlog has 1 pending delivery for actor=x."
TOOLS = [{"name": HARNESS_READ}, {"name": HARNESS_REPLY}, {"name": "bash"}]


def _events(chunks: list[bytes]) -> list[dict[str, object]]:
    events = []
    for frame in b"".join(chunks).decode().strip().split("\n\n"):
        name, data = frame.split("\n")
        payload = json.loads(data.removeprefix("data: "))
        assert name == f"event: {payload['type']}"
        events.append(payload)
    return events


def test_a_wake_with_harness_tools_calls_harness_read() -> None:
    body = {"tools": TOOLS, "messages": [{"role": "user", "content": WAKE}]}

    decision = decide(body, MARKER)

    assert (decision.kind, decision.tool, decision.tool_input) == (
        "tool_use",
        HARNESS_READ,
        {},
    )


def test_a_wake_without_harness_tools_names_the_cause() -> None:
    body = {"tools": [{"name": "bash"}], "messages": [{"role": "user", "content": WAKE}]}

    assert decide(body, MARKER).text == TOOLS_MISSING


def test_a_read_result_is_answered_with_a_reply_to_that_message() -> None:
    read_result = json.dumps(
        {"entries": [{"messageId": "m-1", "summary": "DSH-E2E-abc123 dsh-tui E2E"}]}
    )
    body = {
        "tools": TOOLS,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": WAKE}]},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": HARNESS_READ}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": read_result}],
                    }
                ],
            },
        ],
    }

    decision = decide(body, MARKER)

    assert decision.tool == HARNESS_REPLY
    assert decision.tool_input["messageId"] == "m-1"
    assert "DSH-E2E-abc123" in decision.tool_input["message"]
    assert decision.tool_input["summary"]


def test_a_reply_result_ends_the_turn_and_chatter_gets_text() -> None:
    body = {
        "tools": TOOLS,
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t2", "name": HARNESS_REPLY}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "{}"}],
            },
        ],
    }

    assert decide(body, MARKER).kind == "text"
    assert decide({"messages": [{"role": "user", "content": "hi"}]}, MARKER).text == "ok"


def test_the_stream_carries_usage_closed_blocks_and_a_stop_reason() -> None:
    body = {"tools": TOOLS, "messages": [{"role": "user", "content": WAKE}]}

    events = _events(list(messages_sse(decide(body, MARKER), model="m", call_id="1")))

    assert [event["type"] for event in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert isinstance(events[0]["message"]["usage"], dict)  # type: ignore[index]
    assert events[1]["content_block"]["type"] == "tool_use"  # type: ignore[index]
    assert json.loads(events[2]["delta"]["partial_json"]) == {}  # type: ignore[index]
    assert events[4]["delta"]["stop_reason"] == "tool_use"  # type: ignore[index]


def test_the_server_logs_decisions_without_headers(tmp_path: Path) -> None:
    log = tmp_path / "requests.jsonl"
    body = json.dumps({"tools": TOOLS, "messages": [{"role": "user", "content": WAKE}]})
    with ScriptedMessagesMock(log_path=log, marker_pattern=MARKER) as mock:
        request = urllib.request.Request(
            f"{mock.base_url}/v1/messages",
            data=body.encode(),
            headers={"x-api-key": "synthetic-canary-key", "content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            stream = response.read()
        assert mock.tool_calls(HARNESS_READ) == 1

    assert b"tool_use" in stream
    text = log.read_text()
    assert "synthetic-canary-key" not in text
    assert json.loads(text.splitlines()[0])["harnessTools"] == [HARNESS_READ, HARNESS_REPLY]


def test_fail_wakes_rejects_only_the_first_wake_turns() -> None:
    body = json.dumps({"tools": TOOLS, "messages": [{"role": "user", "content": WAKE}]})
    statuses = []
    with ScriptedMessagesMock(marker_pattern=MARKER, fail_wakes=1) as mock:
        for _attempt in range(2):
            request = urllib.request.Request(
                f"{mock.base_url}/v1/messages", data=body.encode()
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    statuses.append(response.status)
            except urllib.error.HTTPError as error:
                statuses.append(error.code)
        reads = mock.tool_calls(HARNESS_READ)

    assert statuses == [400, 200]
    assert reads == 1
