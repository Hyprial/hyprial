"""Phase B CLI: ``hyprial message conversation list`` over ``conversations.list``.

``--limit`` and ``--cursor`` page the daemon's keyset list unchanged; ``--thread``
is removed.
"""

from __future__ import annotations

import json

import pytest

from hyprial.identity import WorkerSessionBinding
from tests.shell.cli.threads.support import (
    ALICE,
    BOB,
    C_ONE,
    C_TWO,
    OPERATOR,
    ROUTE,
    invoke,
    record,
)


def _head(conversation: str, peer: str, kind: str, at_ms: int, count: int) -> dict:
    return {
        "conversationId": conversation,
        "kind": kind,
        "peer": peer,
        "legacyConversationId": "legacy",
        "lastMessageAtMs": at_ms,
        "lastMessageId": "m-" + conversation[-3:],
        "messages": count,
    }


HEADS = [
    _head(C_ONE, BOB, "agent", 1_700_000_000_000, 3),
    _head(C_TWO, ROUTE, "adapter", 1_699_999_000_000, 1),
]


def _answer(heads: list[dict], cursor: str | None = None) -> dict:
    return {"ok": True, "participant": ALICE, "conversations": heads, "nextCursor": cursor}


def test_list_json_passes_the_answer_through(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _answer(HEADS)
    calls = record(monkeypatch, {"conversations.list": answer})

    result = invoke("conversation", "list", "--on-behalf-of", ALICE, "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == answer
    assert calls == [("conversations.list", {"from": OPERATOR, "onBehalfOf": ALICE})]


def test_list_pages_with_limit_and_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(monkeypatch, {"conversations.list": _answer([])})

    result = invoke(
        "conversation", "list", "--limit", "5", "--cursor", "abc", "--json"
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "conversations.list",
            {"from": OPERATOR, "limit": 5, "cursor": "abc"},
        )
    ]


def test_list_renders_a_table_and_names_the_next_page(monkeypatch: pytest.MonkeyPatch) -> None:
    record(monkeypatch, {"conversations.list": _answer(HEADS, cursor="next-1")})

    result = invoke("conversation", "list", "--on-behalf-of", ALICE)

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == f"conversations of {ALICE}"
    assert lines[1].split() == ["CONVERSATION", "KIND", "PEER", "MESSAGES", "LAST"]
    assert lines[2].split() == [
        C_ONE, "agent", BOB, "3", "2023-11-14T22:13:20Z",
    ]
    assert lines[3].split() == [
        C_TWO, "adapter", ROUTE, "1", "2023-11-14T21:56:40Z",
    ]
    assert lines[-1] == "more: --cursor next-1"


def test_an_empty_list_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    record(monkeypatch, {"conversations.list": {**_answer([]), "participant": OPERATOR}})

    result = invoke("conversation", "list")

    assert result.exit_code == 0, result.output
    assert result.stdout == f"no conversations for {OPERATOR}\n"


def test_a_daemon_refusal_keeps_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from hyprial.shell.impl.cli.commands.common.support import CliError

    def refuse(_params: dict) -> dict:
        raise CliError("SENDER_NOT_AUTHENTICATED", "no session", {"claimedIdentity": ALICE})

    record(monkeypatch, {"conversations.list": refuse})

    result = invoke("conversation", "list", "--from", ALICE, "--json")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False and payload["code"] == "SENDER_NOT_AUTHENTICATED"


def test_the_default_reader_is_the_local_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(monkeypatch, {"conversations.list": _answer([])})

    result = invoke("conversation", "list", "--json")

    assert result.exit_code == 0, result.output
    assert calls == [("conversations.list", {"from": OPERATOR})]


def test_a_verified_session_reads_as_itself_with_its_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = WorkerSessionBinding(ALICE, "session-1")
    calls = record(monkeypatch, {"conversations.list": _answer([])}, binding=binding)

    result = invoke("conversation", "list", "--json")

    assert result.exit_code == 0, result.output
    assert calls == [("conversations.list", {"from": ALICE, "sessionRef": "session-1"})]


def test_conversation_list_rejects_removed_thread_option() -> None:
    result = invoke("conversation", "list", "--thread", BOB)
    assert result.exit_code == 2
