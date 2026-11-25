"""A local model-provider endpoint, so E2E stops measuring the network.

What this is and is not
-----------------------
It impersonates the *provider API* -- the OpenAI and Anthropic wire formats --
and nothing else.  The harnesses (``claude`` / ``codex`` / ``pi``) stay real
processes talking real protocols; only the far side of the socket is ours.
Mocking a harness would mean no longer testing our wiring to it, and harnesses
iterate too fast for a stand-in to stay honest.  Provider wire formats do not.

Why it must stream
------------------
Verified rather than assumed: pointing pi at a plain-JSON endpoint produced
``Stream ended without finish_reason``.  The harnesses request streaming
responses, so a non-streaming fake is not a simpler version of this -- it is a
broken one.

Fault injection
---------------
Returning only well-formed answers would make this a latency fix.  The point
is the failures a real provider cannot be asked to produce on demand:
timeouts, 429s, malformed bodies, and a stream that never terminates.

That last one is deliberate.  This repository's recurring defect is *waiting
for a signal that never arrives* -- a FIFO head never released, a barrier
awaiting a completion that cannot come.  It is also observable: pi reports it
rather than hanging, so a test injecting it can actually go red.  Both axes
matter -- whether the fault is worth testing, and whether the system reacts
visibly enough for the test to fail.

⚠️ Deliberately NOT covered (decided, not overlooked):
  * byte-level truncation mid-SSE-frame
  * connection dropped after N tokens
Both sit closer to the transport layer than to the failure family we have
actually observed.  Recorded here by name so a later reader can tell a
decision from an omission.

⚠️ Retries change what an injection means
-----------------------------------------
pi retries a failed completion (observed: 4 attempts).  "A fault was injected"
and "the fault reached the assertion" are therefore different claims: inject
once and the retry repairs it, leaving a test permanently green on the very
path it claims to cover.  Hence :meth:`fail_first` versus :meth:`fail_always`
-- the caller must say which layer is under assertion.
"""

from __future__ import annotations

import itertools
import json
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

# The real contracts this fake was written against.  A mock silently drifts
# from the service it imitates exactly the way stale evidence drifts from the
# tree it was gathered on -- neither announces it.  Calibration compares these
# against the live versions before comparing any payload, so "out of date" is
# a finding rather than a surprise.
CONTRACT_VERSIONS: dict[str, str] = {
    "openai.chat.completions": "2024-10-01",
    "openai.responses": "2025-04-01",
    "anthropic.messages": "2023-06-01",
    "observed.pi": "pi 0.x via registerProvider baseUrl override",
}

TIMEOUT = "timeout"
RATE_LIMITED = "rate_limited"
MALFORMED = "malformed"
NO_TERMINATOR = "no_terminator"

_FAULTS = frozenset({TIMEOUT, RATE_LIMITED, MALFORMED, NO_TERMINATOR})
_MESSAGE_ID = re.compile(
    r"messageId[^0-9a-f]{1,32}([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


def _walk(value: object) -> Iterator[object]:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _available_tool(
    body: object, suffix: str
) -> tuple[str, str | None] | None:
    tools = body.get("tools") if isinstance(body, dict) else None
    if not isinstance(tools, list):
        return None
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        nested = tool.get("tools")
        candidates = nested if isinstance(nested, list) else (tool,)
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            name = candidate.get("name")
            function = candidate.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                name = function["name"]
            if isinstance(name, str) and name.endswith(suffix):
                namespace = (
                    tool.get("name")
                    if isinstance(nested, list) and isinstance(tool.get("name"), str)
                    else None
                )
                return name, namespace
    return None


def _called_tool(body: object, suffix: str) -> bool:
    conversation = (
        {key: value for key, value in body.items() if key != "tools"}
        if isinstance(body, dict)
        else body
    )
    for item in _walk(conversation):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        function = item.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            name = function["name"]
        if (
            isinstance(name, str)
            and name.endswith(suffix)
            and item.get("type") in {"tool_use", "function_call", "function"}
        ):
            return True
    return False


def _harness_reply_call(
    body: object, reply: str
) -> tuple[str, dict[str, Any], str | None] | None:
    """Return the next bridge-tool call needed to settle a real request.

    Some harness prompts carry the message id directly.  Pi's current prompt
    intentionally carries only the summary, so the model must first call
    ``harness_read`` and then reply using the id in that tool result.  Detect
    prior calls to stop after ``harness_reply`` instead of creating a tool loop.
    """

    if _called_tool(body, "harness_reply"):
        return None
    match = _MESSAGE_ID.search(json.dumps(body))
    reply_tool = _available_tool(body, "harness_reply")
    if reply_tool is not None and match is not None:
        name, namespace = reply_tool
        return (
            name,
            {
                "messageId": match.group(1),
                "summary": reply,
                "message": reply,
            },
            namespace,
        )
    read_tool = _available_tool(body, "harness_read")
    if read_tool is not None and not _called_tool(body, "harness_read"):
        name, namespace = read_tool
        return name, {}, namespace
    return _deferred_reply_tool_search(body, match is not None)


_TOOL_USE_IDS = itertools.count(1)
_DEFERRED_REPLY_TOOL = re.compile(r"mcp__[A-Za-z0-9_-]+__harness_reply")


def _deferred_reply_tool_search(
    body: object, has_message_id: bool
) -> tuple[str, dict[str, Any], str | None] | None:
    """Load a deferred ``harness_reply`` through the harness's ToolSearch.

    Recent Claude Code (the CLI the Agent SDK bundles) defers MCP tools:
    ``tools`` carries only ``ToolSearch`` and the bridge tools are listed by
    name in the conversation.  A real model selects the tool first, then the
    next request carries its schema and the ordinary reply path above takes
    over.  ToolSearch is called once per conversation, never in a loop.
    """

    if not has_message_id or _available_tool(body, "ToolSearch") is None:
        return None
    if _called_tool(body, "ToolSearch"):
        return None
    named = _DEFERRED_REPLY_TOOL.search(json.dumps(body))
    query = f"select:{named.group(0)}" if named is not None else "harness_reply"
    return "ToolSearch", {"query": query, "max_results": 5}, None


class FaultPlan:
    """Which requests get a fault, and which fault.

    ``remaining`` counts down: ``fail_first`` injects once and lets the retry
    succeed; ``fail_always`` never relents.  The distinction is the assertion
    layer, not a convenience.
    """

    __slots__ = ("fault", "remaining")

    def __init__(self, fault: str | None, remaining: int) -> None:
        if fault is not None and fault not in _FAULTS:
            raise ValueError(f"unknown fault {fault!r}; known: {sorted(_FAULTS)}")
        self.fault = fault
        self.remaining = remaining

    @classmethod
    def healthy(cls) -> "FaultPlan":
        return cls(None, 0)

    @classmethod
    def fail_first(cls, fault: str, count: int = 1) -> "FaultPlan":
        """Fail the first ``count`` requests, then serve normally.

        For asserting that a retry recovers *and leaves no duplicate effect*.
        """

        return cls(fault, count)

    @classmethod
    def fail_always(cls, fault: str) -> "FaultPlan":
        """Fail every request, so the harness exhausts its retries.

        For asserting how we handle a *final* provider failure.  Injecting
        once would not reach that assertion.
        """

        return cls(fault, -1)

    def take(self) -> str | None:
        if self.fault is None or self.remaining == 0:
            return None
        if self.remaining > 0:
            self.remaining -= 1
        return self.fault


def _openai_chunks(text: str, *, terminate: bool) -> Iterator[bytes]:
    for piece in text.split(" "):
        payload = {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "model": "fake",
            "choices": [{"index": 0, "delta": {"content": piece + " "}}],
        }
        yield f"data: {json.dumps(payload)}\n\n".encode()
    if not terminate:
        return
    done = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": "fake",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(done)}\n\n".encode()
    yield b"data: [DONE]\n\n"


def _openai_tool_chunks(
    name: str, arguments: dict[str, Any], _namespace: str | None
) -> Iterator[bytes]:
    payload = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": "fake",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_fake",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ]
                },
            }
        ],
    }
    yield f"data: {json.dumps(payload)}\n\n".encode()
    done = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "model": "fake",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }
    yield f"data: {json.dumps(done)}\n\n".encode()
    yield b"data: [DONE]\n\n"


def _responses_chunks(text: str, *, terminate: bool) -> Iterator[bytes]:
    """OpenAI Responses SSE events consumed by the real Codex app-server."""

    response_id = "resp_fake"
    message_id = "msg_fake"
    response = {
        "id": response_id,
        "object": "response",
        "created_at": 1,
        "status": "in_progress",
        "model": "fake",
        "output": [],
        "parallel_tool_calls": True,
        "usage": None,
    }
    events = [
        {"type": "response.created", "response": response},
        {"type": "response.in_progress", "response": response},
        {
            "type": "response.output_item.added",
            "response_id": response_id,
            "output_index": 0,
            "item": {
                "id": message_id,
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            },
        },
        {
            "type": "response.content_part.added",
            "response_id": response_id,
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "response_id": response_id,
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
    ]
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence
        yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
    if not terminate:
        return
    content = {"type": "output_text", "text": text, "annotations": []}
    item = {
        "id": message_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [content],
    }
    completed = {**response, "status": "completed", "output": [item]}
    completed["usage"] = {
        "input_tokens": 1,
        "output_tokens": max(1, len(text.split())),
        "total_tokens": max(2, len(text.split()) + 1),
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
    }
    tail = [
        {"type": "response.output_text.done", "text": text},
        {"type": "response.content_part.done", "part": content},
        {"type": "response.output_item.done", "item": item},
        {"type": "response.completed", "response": completed},
    ]
    for sequence, event in enumerate(tail, start=len(events)):
        event.update(
            {
                "sequence_number": sequence,
                "response_id": response_id,
                "item_id": message_id,
                "output_index": 0,
                "content_index": 0,
            }
        )
        yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


def _responses_tool_chunks(
    name: str, arguments: dict[str, Any], namespace: str | None
) -> Iterator[bytes]:
    response_id = "resp_fake"
    item = {
        "id": "fc_fake",
        "type": "function_call",
        "status": "completed",
        "name": name,
        "call_id": "call_fake",
        "arguments": json.dumps(arguments),
    }
    if namespace is not None:
        item["namespace"] = namespace
    response = {
        "id": response_id,
        "object": "response",
        "created_at": 1,
        "status": "in_progress",
        "model": "fake",
        "output": [],
        "parallel_tool_calls": True,
        "usage": None,
    }
    events = [
        {"type": "response.created", "response": response},
        {"type": "response.in_progress", "response": response},
        {
            "type": "response.output_item.added",
            "response_id": response_id,
            "output_index": 0,
            "item": {**item, "status": "in_progress", "arguments": ""},
        },
        {
            "type": "response.function_call_arguments.delta",
            "response_id": response_id,
            "item_id": item["id"],
            "output_index": 0,
            "delta": item["arguments"],
        },
        {
            "type": "response.function_call_arguments.done",
            "response_id": response_id,
            "item_id": item["id"],
            "output_index": 0,
            "arguments": item["arguments"],
        },
        {
            "type": "response.output_item.done",
            "response_id": response_id,
            "output_index": 0,
            "item": item,
        },
    ]
    completed = {
        **response,
        "status": "completed",
        "output": [item],
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    events.append({"type": "response.completed", "response": completed})
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence
        yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


# KP-017: a complete Messages API ``message_start``.  The Claude Agent SDK
# rejects an assistant message without ``model`` (MessageParseError), so the
# synthetic stream carries every field a real provider sends.
_ANTHROPIC_MESSAGE_START = {
    "id": "msg_fake",
    "type": "message",
    "role": "assistant",
    "model": "loopback-model",
    "content": [],
    "stop_reason": None,
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


def _anthropic_chunks(text: str, *, terminate: bool) -> Iterator[bytes]:
    start = {
        "type": "message_start",
        "message": _ANTHROPIC_MESSAGE_START,
    }
    yield f"event: message_start\ndata: {json.dumps(start)}\n\n".encode()
    block = {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}}
    yield f"event: content_block_start\ndata: {json.dumps(block)}\n\n".encode()
    for piece in text.split(" "):
        delta = {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": piece + " "}}
        yield f"event: content_block_delta\ndata: {json.dumps(delta)}\n\n".encode()
    if not terminate:
        return
    yield (
        b'event: content_block_stop\ndata: {"type":"content_block_stop",'
        b'"index":0}\n\n'
    )
    message_delta = {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 1},
    }
    yield f"event: message_delta\ndata: {json.dumps(message_delta)}\n\n".encode()
    yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


def _anthropic_tool_chunks(
    name: str, arguments: dict[str, Any], _namespace: str | None
) -> Iterator[bytes]:
    start = {
        "type": "message_start",
        "message": _ANTHROPIC_MESSAGE_START,
    }
    yield f"event: message_start\ndata: {json.dumps(start)}\n\n".encode()
    block = {
        "type": "content_block_start",
        "index": 0,
        "content_block": {
            "type": "tool_use",
            # One id per tool_use: a conversation that loads a deferred tool
            # (ToolSearch) and then calls it holds two tool_use blocks, and
            # Claude Code fails a turn whose tool_use ids repeat.
            "id": f"toolu_fake_{next(_TOOL_USE_IDS)}",
            "name": name,
            "input": {},
        },
    }
    yield f"event: content_block_start\ndata: {json.dumps(block)}\n\n".encode()
    delta = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {
            "type": "input_json_delta",
            "partial_json": json.dumps(arguments),
        },
    }
    yield f"event: content_block_delta\ndata: {json.dumps(delta)}\n\n".encode()
    yield (
        b'event: content_block_stop\ndata: {"type":"content_block_stop",'
        b'"index":0}\n\n'
    )
    message_delta = {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
        "usage": {"output_tokens": 1},
    }
    yield f"event: message_delta\ndata: {json.dumps(message_delta)}\n\n".encode()
    yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


class _Handler(BaseHTTPRequestHandler):
    server: "FakeProvider"  # type: ignore[assignment]

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        provider = self.server  # type: ignore[assignment]
        length = int(self.headers.get("content-length", "0") or 0)
        raw_body = self.rfile.read(length)
        provider.requests.append(self.path)
        try:
            request_body: object = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            request_body = raw_body.decode(errors="replace")
        provider.request_bodies.append(request_body)

        fault = provider.plan.take()
        if fault == TIMEOUT:
            # Never answer.  The caller's own deadline is the assertion; a
            # fake that closed the socket would be testing a different thing.
            provider.timed_out.set()
            return
        if fault == RATE_LIMITED:
            body = b'{"error":{"type":"rate_limit_error","message":"slow down"}}'
            self.send_response(429)
            self.send_header("content-type", "application/json")
            self.send_header("retry-after", "1")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if fault == MALFORMED:
            body = b"{not json at all"
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        terminate = fault != NO_TERMINATOR
        anthropic = "/messages" in self.path
        responses = "/responses" in self.path
        tool_call = (
            _harness_reply_call(request_body, provider.reply)
            if provider.reply_to_harness_requests and terminate
            else None
        )
        if anthropic:
            chunks = (
                _anthropic_tool_chunks(*tool_call)
                if tool_call is not None
                else _anthropic_chunks(provider.reply, terminate=terminate)
            )
        elif responses:
            chunks = (
                _responses_tool_chunks(*tool_call)
                if tool_call is not None
                else _responses_chunks(provider.reply, terminate=terminate)
            )
        else:
            chunks = (
                _openai_tool_chunks(*tool_call)
                if tool_call is not None
                else _openai_chunks(provider.reply, terminate=terminate)
            )
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(chunk)
            self.wfile.flush()

    def log_message(self, *_args: Any) -> None:
        return


class FakeProvider(HTTPServer):
    """A provider endpoint under the test's control.

    Use as a context manager; ``openai_base_url`` / ``anthropic_base_url`` are
    what the harness should be pointed at.
    """

    def __init__(
        self,
        *,
        reply: str = "ok",
        plan: FaultPlan | None = None,
        reply_to_harness_requests: bool = False,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.reply = reply
        self.plan = plan or FaultPlan.healthy()
        self.reply_to_harness_requests = reply_to_harness_requests
        self.requests: list[str] = []
        self.request_bodies: list[object] = []
        self.timed_out = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def openai_base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def anthropic_base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "FakeProvider":
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        # Stop the loop, wait for it to actually leave serve_forever, and
        # only then close the socket -- closing a socket the loop may still
        # be selecting on is a race, not a tidier ordering.
        #
        # (This ordering does not fix the one AF_UNIX descriptor a threaded
        # serve_forever creates: measured across four cycles, the first adds
        # one and the rest add none, so it is one-time process state, warmed
        # in tests/conftest.py rather than chased here.)
        self.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)
            # Asserted rather than best-effort: a teardown that can fail
            # quietly is the leak it exists to prevent, only harder to find.
            assert not self._thread.is_alive(), "fake provider thread did not stop"
        self.server_close()
