"""Group messages through the existing ``message send`` and ``message history``.

``message send --conversation <g.>`` sends to the group with no ``--to``;
``--mention`` names members by name or member id, mapped to member ids by
one ``conversations.get``; ``--reply-to`` answers a group message.  ``message
history --conversation <g.>`` reads the group document and renders authors,
mentions, replies and membership changes by name.  ``--json`` stays the
daemon answer.  The direct path is pinned in
``tests/shell/cli/threads/test_cli_direct_unchanged.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hyprial.daemon.impl.request_catalog.conversations.group_examples import (
    GROUP_HISTORY_CASES,
)
from hyprial.daemon.impl.request_catalog.message.send.specs import MESSAGE_SEND_SPEC
from hyprial.shell.impl.cli.commands.common.support import CliError
from tests.shell.cli.groups.support import (
    GROUP_ID,
    OPERATOR,
    REVIEWER_ID,
    WORKER_ID,
    assert_declared_params,
    example,
    record,
)
from tests.shell.cli.threads.support import invoke as invoke_message

GROUP = example("conversations.get", "release-group")
ASKED = "8a6a4329-6c56-52bd-bf24-4f2dfe08022a"
POSTED = "4adbe4a7-0c35-5e04-95ea-f5b7ffca27b4"
SENT = {
    "ok": True,
    "conversationId": GROUP_ID,
    "deliveries": [
        {"target": GROUP_ID, "messageId": ASKED, "accepted": True, "queued": False},
    ],
}


def _page() -> dict[str, Any]:
    [case] = [case for case in GROUP_HISTORY_CASES if case.name == "group-page"]
    return json.loads(json.dumps(case.result, default=dict))


def _send(*args: str) -> Any:
    return invoke_message(
        "send", "--from", OPERATOR, "--conversation", GROUP_ID, "--summary", "release", *args
    )


def test_a_group_send_has_no_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(monkeypatch, {"message.send": SENT})

    result = _send("please", "cut", "the", "release")

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "message.send",
            {
                "from": OPERATOR,
                "message": "please cut the release",
                "summary": "release",
                "conversationId": GROUP_ID,
            },
        )
    ]
    assert result.stdout == f"sent    {GROUP_ID}  8a6a…\n"


def test_mentions_are_named_and_sent_as_member_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(monkeypatch, {"conversations.get": GROUP, "message.send": SENT})

    result = _send("--mention", "reviewer", "--mention", WORKER_ID, "check", "the", "notes")

    assert result.exit_code == 0, result.output
    assert calls[0] == ("conversations.get", {"from": OPERATOR, "conversationId": GROUP_ID})
    method, params = calls[1]
    assert method == "message.send"
    assert params["mentions"] == [REVIEWER_ID, WORKER_ID]
    assert "to" not in params
    assert_declared_params(calls[:1])


def test_a_mention_that_names_no_member_is_sent_as_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = record(monkeypatch, {"conversations.get": GROUP, "message.send": SENT})

    assert _send("--mention", "ghost", "hello").exit_code == 0

    assert calls[1][1]["mentions"] == ["ghost"]


def test_reply_to_answers_a_group_message(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(monkeypatch, {"message.send": SENT})

    result = _send("--reply-to", ASKED, "tag", "is", "pushed")

    assert result.exit_code == 0, result.output
    assert calls[0][1]["replyTo"] == ASKED
    assert "to" not in calls[0][1]


def test_group_send_json_is_the_daemon_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    record(monkeypatch, {"message.send": SENT})

    result = _send("--json", "hello")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == SENT


@pytest.mark.parametrize("code", ["CONVERSATION_NOT_FOUND", "CONVERSATION_NOT_MEMBER"])
def test_group_send_refusals_keep_their_code(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    assert code in {error.code for error in MESSAGE_SEND_SPEC.errors}

    def refuse(_params: dict) -> Any:
        raise CliError(code, f"refused with {code}", {"conversationId": GROUP_ID})

    record(monkeypatch, {"message.send": refuse})

    result = _send("--json", "hello")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == code and payload["data"] == {"conversationId": GROUP_ID}


def test_group_history_renders_names_mentions_and_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = record(
        monkeypatch, {"conversations.history": _page(), "conversations.get": GROUP}
    )

    result = invoke_message("history", "--conversation", GROUP_ID)

    assert result.exit_code == 0, result.output
    assert ("conversations.history", {"from": OPERATOR, "conversationId": GROUP_ID}) in calls
    assert ("conversations.get", {"from": OPERATOR, "conversationId": GROUP_ID}) in calls
    assert result.stdout == (
        '2026-09-21T14:15:20Z  alice created the group "release"\n'
        "2026-09-21T14:15:20Z  alice added worker\n"
        "2026-09-21T14:15:20Z  alice added reviewer\n"
        f"2026-09-21T14:16:20Z  alice  {ASKED}\n"
        "  please cut the release\n"
        f"2026-09-21T14:17:20Z  alice  {POSTED}\n"
        f'  in reply to alice {ASKED}: "please cut the release"\n'
        "  mentions: worker\n"
        "  please tag the release\n"
        "2026-09-21T14:18:20Z  worker  b602ca69-3f2b-51ff-ac6b-4aa7a54cde63\n"
        f'  in reply to alice {POSTED}: "please tag the release"\n'
        "  tag is pushed\n"
    )


def test_group_history_names_a_former_member_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _page()
    page["entries"] = page["entries"][-1:]
    page["nextOlderCursor"] = "old-1"
    current = {**GROUP, "members": [m for m in GROUP["members"] if m["memberId"] != WORKER_ID]}
    record(monkeypatch, {"conversations.history": page, "conversations.get": current})

    result = invoke_message("history", "--conversation", GROUP_ID)

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split()[1] == WORKER_ID
    assert lines[-1] == f"older: --conversation {GROUP_ID} --mode older --cursor old-1"


def test_group_history_json_is_the_daemon_page(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(monkeypatch, {"conversations.history": _page()})

    result = invoke_message("history", "--conversation", GROUP_ID, "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == _page()
    assert [method for method, _params in calls] == ["conversations.history"]


@pytest.mark.parametrize("code", ["CONVERSATION_NOT_FOUND", "CONVERSATION_NOT_MEMBER"])
def test_group_history_refusals_keep_their_code(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    def refuse(_params: dict) -> Any:
        raise CliError(code, f"refused with {code}", {"conversationId": GROUP_ID})

    record(monkeypatch, {"conversations.history": refuse, "conversations.get": refuse})

    result = invoke_message("history", "--conversation", GROUP_ID)

    assert result.exit_code == 1
    assert f"refused with {code}" in result.output


def test_reply_resolves_mentions_and_passes_no_mention(monkeypatch: pytest.MonkeyPatch) -> None:
    replied = {"ok": True, "messageId": "r-1", "replied": True, "acknowledged": True, "queued": False}
    agents = {
        "ok": True,
        "agents": [
            {"actor": "reviewer", "agentId": REVIEWER_ID, "displayName": "reviewer"},
            {"actor": "worker", "agentId": WORKER_ID, "displayName": "worker"},
        ],
    }
    calls = record(monkeypatch, {"agent.list": agents, "message.reply": replied})

    result = invoke_message(
        "reply",
        "row-1",
        "checking",
        "--from",
        WORKER_ID,
        "--summary",
        "checked",
        "--mention",
        "reviewer",
        "--mention",
        WORKER_ID,
        "--no-mention",
        "--json",
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        ("agent.list", {"includeUserAgent": True}),
        (
            "message.reply",
            {
                "from": WORKER_ID,
                "messageId": "row-1",
                "message": "checking",
                "summary": "checked",
                "mentions": [REVIEWER_ID, WORKER_ID],
                "noMention": True,
            },
        ),
    ]
    assert json.loads(result.stdout) == replied


def test_reply_with_member_id_mentions_skips_agent_list(monkeypatch: pytest.MonkeyPatch) -> None:
    replied = {"ok": True, "messageId": "r-1", "replied": True, "acknowledged": True, "queued": False}
    calls = record(monkeypatch, {"message.reply": replied})

    result = invoke_message(
        "reply",
        "row-1",
        "checking",
        "--from",
        WORKER_ID,
        "--summary",
        "checked",
        "--mention",
        REVIEWER_ID,
        "--json",
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "message.reply",
            {
                "from": WORKER_ID,
                "messageId": "row-1",
                "message": "checking",
                "summary": "checked",
                "mentions": [REVIEWER_ID],
            },
        ),
    ]
