"""Phase B CLI: ``hyprial message history --conversation`` (design §9.5).

``--conversation <c.>`` is one ``conversations.history`` page (``--mode``,
``--cursor`` and ``--limit`` pass through).  The thread view of §9.5 is
``message conversation list --thread <thread>`` followed by this command per
conversation: a merged per-thread history is a read model, so it waits for a
daemon method and the CLI publishes no ``--thread`` on ``message history``.
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


def _history(conversation: str, entries: list[dict], older: str | None = None) -> dict:
    return {
        "ok": True,
        "conversationId": conversation,
        "mode": "tail",
        "entries": entries,
        "headCursor": None,
        "nextCursor": None,
        "nextOlderCursor": older,
        "legacyConversationId": "legacy",
    }


ONE = [
    entry_row("m1", 1_000, ALICE, BOB, "first"),
    entry_row("m3", 3_000, BOB, ALICE, "third\nsecond line"),
]


def test_conversation_history_json_is_the_daemon_page(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _history(C_ONE, ONE)
    calls = record(monkeypatch, {"conversations.history": answer})

    result = invoke("history", "--conversation", C_ONE, "--on-behalf-of", ALICE, "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == answer
    assert calls == [
        ("conversations.history", {"from": OPERATOR, "onBehalfOf": ALICE, "conversationId": C_ONE})
    ]


def test_conversation_paging_flags_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(monkeypatch, {"conversations.history": _history(C_ONE, [])})

    result = invoke(
        "history", "--conversation", C_ONE, "--mode", "older", "--cursor", "k1",
        "--limit", "10", "--json",
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "conversations.history",
            {"from": OPERATOR, "conversationId": C_ONE, "mode": "older", "cursor": "k1", "limit": 10},
        )
    ]


def test_history_renders_one_block_per_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    record(monkeypatch, {"conversations.history": _history(C_ONE, ONE, older="o-1")})

    result = invoke("history", "--conversation", C_ONE, "--on-behalf-of", ALICE)

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"1970-01-01T00:00:01Z  {ALICE} -> {BOB}  request  m1",
        "  first",
        f"1970-01-01T00:00:03Z  {BOB} -> {ALICE}  request  m3",
        "  third",
        "  second line",
        f"older: --conversation {C_ONE} --mode older --cursor o-1",
    ]


def test_an_empty_history_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    record(monkeypatch, {"conversations.history": _history(C_ONE, [])})

    result = invoke("history", "--conversation", C_ONE)

    assert result.exit_code == 0, result.output
    assert result.stdout == "no messages\n"


def test_history_without_a_conversation_is_an_invalid_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = record(monkeypatch, {})

    result = invoke("history", "--json")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False and payload["code"] == "INVALID_ARGUMENT"
    assert "message conversation list" in payload["error"]
    assert calls == []


def test_history_publishes_no_thread_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(monkeypatch, {})

    help_text = invoke("history", "--help")
    refused = invoke("history", "--thread", "t.alice-bob", "--json")

    assert help_text.exit_code == 0, help_text.output
    assert "--thread" not in help_text.output
    assert refused.exit_code == 2
    assert calls == []


def test_the_older_hint_can_be_run_as_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(
        monkeypatch,
        {"conversations.history": lambda params: _history(C_ONE, ONE, older="o-1")},
    )

    first = invoke("history", "--conversation", C_ONE)
    hint = first.stdout.splitlines()[-1].removeprefix("older: ").split()
    again = invoke("history", *hint)

    assert again.exit_code == 0, again.output
    assert calls[-1] == (
        "conversations.history",
        {"from": OPERATOR, "conversationId": C_ONE, "mode": "older", "cursor": "o-1"},
    )
