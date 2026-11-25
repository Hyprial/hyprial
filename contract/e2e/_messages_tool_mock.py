"""Scripted Anthropic-Messages endpoint that drives Harness MCP tool calls.

Why this exists
---------------
``_fake_provider`` answers every request with streamed text. That proves a
harness reached a model, but not that the model could reach Hyprial: a wake
turn acceptance check has to show the wake turn calling ``harness_read`` and
``harness_reply`` through the launch-scoped MCP server. Without a provider
that emits ``tool_use`` that step could only ever be BLOCKED, and a real key
is out of bounds for E2E.

What it does
------------
Each request is answered from the request alone (no hidden conversation
state), so a resumed or retried turn gets the same decision:

* the last user message carries a ``harness_read`` tool result naming a
  pending ``messageId`` -> call ``harness_reply`` for it, echoing the first
  marker found in that result;
* the last user message carries a ``harness_reply`` result -> end the turn;
* the last user message is a Harness wake (``[Harness Network]``) and the
  request offers ``harness_read`` -> call it;
* a wake whose request offers no Harness tool -> answer the fixed text
  ``HARNESS_TOOLS_MISSING`` so the runner can report the cause;
* anything else (titles, summaries, user chatter) -> a short text answer.

``fail_wakes=N`` answers the first N wake requests with HTTP 400 instead, so
a runner can prove a failed wake turn is reported (``wake.failed``) and
retried rather than logged as signalled.

The wire shape is the Anthropic Messages stream: ``message_start`` carries a
usage object, every block is closed, and ``message_delta`` names a stop reason
before ``message_stop``.

Every request is appended to ``log_path`` as one JSON line holding only the
offered tool names, the decision and the tool called -- never headers, so the
synthetic key never reaches disk.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HARNESS_SERVER = "harness-bridge"
HARNESS_TOOL_PREFIX = f"mcp__{HARNESS_SERVER}__"
HARNESS_READ = f"{HARNESS_TOOL_PREFIX}harness_read"
HARNESS_REPLY = f"{HARNESS_TOOL_PREFIX}harness_reply"
WAKE_PREFIX = "[Harness Network]"
TOOLS_MISSING = "HARNESS_TOOLS_MISSING"
_MESSAGE_ID = re.compile(r'"messageId"\s*:\s*"([^"\\]+)"')


@dataclass(frozen=True)
class Decision:
    """One scripted model answer: plain text, or exactly one tool call."""

    kind: str
    text: str = ""
    tool: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict)
        )
    return ""


def _tool_names_by_id(messages: list[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for block in _blocks(message):
            if block.get("type") == "tool_use":
                names[str(block.get("id"))] = str(block.get("name"))
    return names


def decide(body: dict[str, Any], marker_pattern: str) -> Decision:
    """Pick the scripted answer for one Messages request body."""

    tools = {
        str(tool.get("name"))
        for tool in body.get("tools") or []
        if isinstance(tool, dict)
    }
    messages = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    if not messages or messages[-1].get("role") != "user":
        return Decision("text", text="ok")
    last = _blocks(messages[-1])
    names = _tool_names_by_id(messages)
    for block in last:
        if block.get("type") != "tool_result":
            continue
        name = names.get(str(block.get("tool_use_id")), "")
        text = _result_text(block)
        if name == HARNESS_READ:
            ids = _MESSAGE_ID.findall(text)
            if not ids:
                return Decision("text", text="no pending Harness message")
            found = re.search(marker_pattern, text)
            marker = found.group(0) if found else "NO-MARKER"
            return Decision(
                "tool_use",
                tool=HARNESS_REPLY,
                tool_input={
                    "messageId": ids[0],
                    "summary": f"{marker} reply",
                    "message": f"{marker} answered by the dsh-tui E2E mock",
                },
            )
        if name == HARNESS_REPLY:
            return Decision("text", text="Harness reply sent.")
    wake = any(
        block.get("type") == "text"
        and str(block.get("text", "")).startswith(WAKE_PREFIX)
        for block in last
    )
    if wake:
        if HARNESS_READ in tools:
            return Decision("tool_use", tool=HARNESS_READ, tool_input={})
        return Decision("text", text=TOOLS_MISSING)
    return Decision("text", text="ok")


def _event(name: str, payload: dict[str, Any]) -> bytes:
    payload = {"type": name, **payload}
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()


def messages_sse(decision: Decision, *, model: str, call_id: str) -> Iterator[bytes]:
    """Encode one decision as a complete Messages SSE stream."""

    yield _event(
        "message_start",
        {
            "message": {
                "id": f"msg_{call_id}",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "usage": {"input_tokens": 1, "output_tokens": 0},
            }
        },
    )
    if decision.kind == "tool_use":
        block = {
            "type": "tool_use",
            "id": f"toolu_{call_id}",
            "name": decision.tool,
            "input": {},
        }
        delta = {
            "type": "input_json_delta",
            "partial_json": json.dumps(decision.tool_input),
        }
        stop_reason = "tool_use"
    else:
        block = {"type": "text", "text": ""}
        delta = {"type": "text_delta", "text": decision.text}
        stop_reason = "end_turn"
    yield _event("content_block_start", {"index": 0, "content_block": block})
    yield _event("content_block_delta", {"index": 0, "delta": delta})
    yield _event("content_block_stop", {"index": 0})
    yield _event(
        "message_delta",
        {"delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 1}},
    )
    yield _event("message_stop", {})


class _Handler(BaseHTTPRequestHandler):
    server: "ScriptedMessagesMock"  # type: ignore[assignment]

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        length = int(self.headers.get("content-length", "0") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        decision = decide(body, self.server.marker_pattern)
        if decision.tool == HARNESS_READ and self.server.take_wake_failure():
            self.server.record(self.path, body, Decision("error", text="injected"))
            payload = json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "mock: injected wake-turn failure",
                    },
                }
            ).encode()
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        call_id = self.server.record(self.path, body, decision)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        for chunk in messages_sse(
            decision, model=str(body.get("model") or "mock"), call_id=call_id
        ):
            self.wfile.write(chunk)
            self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        self.send_response(404)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *_args: Any) -> None:
        return


class ScriptedMessagesMock(ThreadingHTTPServer):
    """Loopback Messages endpoint; use as a context manager."""

    daemon_threads = True

    def __init__(
        self,
        *,
        log_path: Path | None = None,
        marker_pattern: str = r"\S+-E2E-\w+",
        fail_wakes: int = 0,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.log_path = log_path
        self.marker_pattern = marker_pattern
        self._wake_failures_left = fail_wakes
        self.records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{int(self.server_address[1])}"

    def record(self, path: str, body: dict[str, Any], decision: Decision) -> str:
        offered = sorted(
            str(tool.get("name"))
            for tool in body.get("tools") or []
            if isinstance(tool, dict)
        )
        with self._lock:
            call_id = f"{len(self.records) + 1:04d}"
            entry = {
                "n": len(self.records) + 1,
                "path": path,
                "harnessTools": [n for n in offered if n.startswith(HARNESS_TOOL_PREFIX)],
                "toolCount": len(offered),
                "decision": decision.kind,
                "tool": decision.tool or None,
                "text": decision.text or None,
            }
            self.records.append(entry)
            if self.log_path is not None:
                with self.log_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(entry) + "\n")
        return call_id

    def take_wake_failure(self) -> bool:
        with self._lock:
            if self._wake_failures_left <= 0:
                return False
            self._wake_failures_left -= 1
            return True

    def tool_calls(self, name: str) -> int:
        with self._lock:
            return sum(1 for entry in self.records if entry["tool"] == name)

    def __enter__(self) -> "ScriptedMessagesMock":
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.server_close()
