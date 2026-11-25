"""Direct (1:1) ``message send``, ``message reply`` and ``message history`` pins.

Written before group sending and group history joined these commands
(slice GC5b): each case pins the exact daemon params and the exact output
of a direct call, so the group additions provably leave the direct path
byte-identical.  The missing ``--to`` case pins Typer's own usage error.
"""

from __future__ import annotations

import json

import pytest

from tests.shell.cli.threads.support import (
    ALICE,
    BOB,
    C_ONE,
    OPERATOR,
    entry_row,
    invoke,
    record,
)

SENT = {
    "ok": True,
    "conversationId": C_ONE,
    "deliveries": [
        {"target": BOB, "messageId": "9f3a1c2e-0000", "accepted": True, "queued": False},
    ],
}
MISSING_TO = (
    "Usage: root message send [OPTIONS] {message}...\n"
    "Try 'root message send --help' for help.\n"
    "\u256d\u2500 Error " + "\u2500" * 70 + "\u256e\n"
    "\u2502 Missing option '--to'." + " " * 55 + "\u2502\n"
    "\u2570" + "\u2500" * 78 + "\u256f\n"
)
REPLIED = {"ok": True, "messageId": "r-1", "acked": True}
PAGE = {
    "ok": True,
    "conversationId": C_ONE,
    "mode": "tail",
    "entries": [
        entry_row("m1", 1_000, ALICE, BOB, "first"),
        entry_row("m3", 3_000, BOB, ALICE, "third\nsecond line"),
    ],
    "headCursor": None,
    "nextCursor": None,
    "nextOlderCursor": "old-1",
    "legacyConversationId": "legacy",
}


def test_direct_send_params_and_output(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(monkeypatch, {"message.send": SENT})

    result = invoke("send", "--from", OPERATOR, "--to", BOB, "--summary", "hi", "hello", "bob")

    assert result.exit_code == 0, result.output
    assert calls == [
        ("message.send", {"from": OPERATOR, "to": [BOB], "message": "hello bob", "summary": "hi"})
    ]
    assert result.stdout == f"sent    {BOB}  9f3a…\n"


def test_direct_send_with_conversation_and_reply_to(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(monkeypatch, {"message.send": SENT})

    result = invoke(
        "send", "--from", OPERATOR, "--to", BOB, "--summary", "hi",
        "--conversation", C_ONE, "--reply-to", "m1", "--json", "hello",
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "message.send",
            {
                "from": OPERATOR, "to": [BOB], "message": "hello", "summary": "hi",
                "conversationId": C_ONE, "replyTo": "m1",
            },
        )
    ]
    assert json.loads(result.stdout) == SENT


def test_send_without_to_or_conversation_is_typers_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = record(monkeypatch, {})

    result = invoke("send", "--from", OPERATOR, "--summary", "hi", "hello")

    assert result.exit_code == 2
    assert calls == []
    assert result.output == MISSING_TO


def test_send_with_a_direct_conversation_still_needs_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = record(monkeypatch, {})

    result = invoke("send", "--from", OPERATOR, "--summary", "hi", "--conversation", C_ONE, "x")

    assert result.exit_code == 2
    assert calls == []
    assert result.output == MISSING_TO


def test_direct_reply_params_and_output(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(monkeypatch, {"message.reply": REPLIED})

    result = invoke("reply", "m1", "done", "--from", ALICE, "--summary", "ok", "--json")

    assert result.exit_code == 0, result.output
    assert calls == [
        ("message.reply", {"from": ALICE, "messageId": "m1", "message": "done", "summary": "ok"})
    ]
    assert json.loads(result.stdout) == REPLIED


def test_direct_history_params_and_output(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(monkeypatch, {"conversations.history": PAGE})

    result = invoke("history", "--conversation", C_ONE)

    assert result.exit_code == 0, result.output
    assert calls == [("conversations.history", {"from": OPERATOR, "conversationId": C_ONE})]
    assert result.stdout == (
        "1970-01-01T00:00:01Z  agent:op:node-1:alice -> agent:op:node-1:bob  request  m1\n"
        "  first\n"
        "1970-01-01T00:00:03Z  agent:op:node-1:bob -> agent:op:node-1:alice  request  m3\n"
        "  third\n"
        "  second line\n"
        f"older: --conversation {C_ONE} --mode older --cursor old-1\n"
    )
