"""Shared fakes for the ``hyprial message conversation|history`` CLI tests.

The daemon request port is replaced by a scripted fake: each test names the
answers per method and reads back the exact calls, so the tests pin which
public daemon methods the commands call and with which params.  No daemon
and no IPC client is involved here; the end-to-end run lives under
``tests/integration/cli/threads``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.identity import WorkerSessionBinding
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry as entry

OWNER = "op"
OPERATOR = f"user:{OWNER}"
ALICE = "agent:op:node-1:alice"
BOB = "agent:op:node-1:bob"
ROUTE = "route:lark:chat-1"
C_ONE = "c." + "d" * 26
C_TWO = "c." + "e" * 26

Answer = Any | Callable[[dict[str, Any]], Any]


def entry_row(message_id: str, at_ms: int, sender: str, to: str, text: str) -> dict[str, Any]:
    """One ``conversations.history`` entry."""

    return {
        "kind": "message",
        "messageId": message_id,
        "conversationId": "legacy",
        "from": sender,
        "to": to,
        "intent": "request",
        "createdAtMs": at_ms,
        "message": text,
        "origin": {},
    }


def record(
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[str, Answer],
    *,
    binding: WorkerSessionBinding | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Install the fake request port and the session binding; return the calls."""

    calls: list[tuple[str, dict[str, Any]]] = []

    def request(method: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> Any:
        sent = dict(params or {})
        calls.append((method, sent))
        if method not in answers:
            raise AssertionError(f"unexpected daemon method {method!r}")
        answer = answers[method]
        return answer(sent) if callable(answer) else answer

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    monkeypatch.setattr(entry, "worker_session_binding", lambda: binding)
    monkeypatch.setenv("HYPRIAL_OWNER", OWNER)
    return calls


def invoke(*args: str) -> Any:
    return CliRunner().invoke(_cli_root_app, ["message", *args])
