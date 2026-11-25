"""Who gets a group row, and that the row wakes it (slice GC4; 2026-11-21 ruling).

A group message wakes ONLY the agent members listed in its ``mentions``,
whether the author is the human or an agent; a reply without ``mentions``
wakes nobody.  The author never gets a row, and a member the rule does not
wake gets NO row at all.  Each woken local agent member gets exactly one
pending row, and that row is the ordinary pending work the daemon wakes an
agent for.
"""

from __future__ import annotations

import json

from tests.daemon.conversations.group_delivery.delivery_support import (
    OWNER,
    batch,
    group_daemon,
    group_rows,
    handle,
    make_group,
    messages,
    row_ids,
    send,
    sweep,
    woken,
    write_unannounced,
)
from tests.support.golden.app import golden_daemon as golden_daemon


def test_an_owner_message_without_mentions_gives_no_rows(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta"))

    send(booted, group, "please cut the release", key="wake-owner-quiet")

    for name in ("alpha", "beta", "gamma"):
        assert group_rows(booted, name) == [], name
        assert not woken(booted, name), name
    assert [entry["message"] for entry in messages(booted, group)] == [
        "please cut the release",
    ]


def test_an_owner_mention_wakes_only_the_mentioned_agent(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta", "gamma"))

    sent = send(booted, group, "alpha, please cut the release", key="wake-owner-1",
                mentions=[booted.agent_id("alpha")])

    rows = group_rows(booted, "alpha", group)
    assert len(rows) == 1
    row = rows[0]
    assert (row.conversation_id, row.sender, row.recipient, row.intent) == (
        group, OWNER, booted.address("alpha"), "request",
    )
    payload = json.loads(row.payload)
    assert payload["groupMessageId"] == sent["messageId"]
    assert payload["message"] == "alpha, please cut the release"
    assert payload["mentions"] == [booted.agent_id("alpha")]
    assert row.summary == "alpha, please cut the release"
    assert woken(booted, "alpha")
    for name in ("beta", "gamma"):
        assert group_rows(booted, name) == [], name
        assert not woken(booted, name), name

    send(booted, group, "both of you", key="wake-owner-2",
         mentions=[booted.agent_id("alpha"), booted.agent_id("beta")])
    assert len(group_rows(booted, "alpha", group)) == 2
    assert len(group_rows(booted, "beta", group)) == 1
    assert set(row_ids(booted, "alpha")).isdisjoint(row_ids(booted, "beta"))


def test_an_agent_message_without_mentions_gives_no_rows(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta", "gamma"))

    send(booted, group, "status: tests pass", "alpha", key="wake-quiet-1")

    for name in ("alpha", "beta", "gamma"):
        assert group_rows(booted, name) == [], name
        assert not woken(booted, name), name


def test_an_agent_mention_wakes_only_the_mentioned_agent(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta", "gamma"))

    send(booted, group, "beta, check the notes", "alpha", key="wake-mention-1",
         mentions=[booted.agent_id("beta")])

    assert len(group_rows(booted, "beta", group)) == 1
    assert group_rows(booted, "beta", group)[0].sender == booted.address("alpha")
    assert group_rows(booted, "alpha") == []
    assert group_rows(booted, "gamma") == []
    assert woken(booted, "beta") and not woken(booted, "gamma")


def test_a_reply_to_an_agent_entry_without_mentions_wakes_nobody(
    golden_daemon, monkeypatch
) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta", "gamma"))
    asked = send(booted, group, "who owns the notes?", "alpha", key="wake-ask-1")
    booted.advance()

    send(booted, group, "I do", "beta", key="wake-answer-1", replyTo=asked["messageId"])

    for name in ("alpha", "beta", "gamma"):
        assert group_rows(booted, name) == [], name
        assert not woken(booted, name), name


def test_a_reply_with_an_explicit_mention_wakes_only_the_mentioned_agent(
    golden_daemon, monkeypatch
) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta", "gamma"))
    asked = send(booted, group, "who owns the notes?", "alpha", key="wake-ask-2")
    booted.advance()

    send(booted, group, "I do", "beta", key="wake-answer-2",
         replyTo=asked["messageId"], mentions=[booted.agent_id("alpha")])

    rows = group_rows(booted, "alpha", group)
    assert len(rows) == 1
    assert rows[0].intent == "reply"
    assert json.loads(rows[0].payload)["replyTo"]["messageId"] == asked["messageId"]
    assert group_rows(booted, "beta") == []
    assert group_rows(booted, "gamma") == []


def test_the_author_never_gets_a_row(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta"))

    send(booted, group, "mentioning myself", "alpha", key="wake-self-1",
         mentions=[booted.agent_id("alpha"), booted.agent_id("beta")])

    assert group_rows(booted, "alpha") == []
    assert len(group_rows(booted, "beta", group)) == 1
    assert list(booted.app._inbox.pending_messages(OWNER)) == []


def test_a_removed_member_gets_nothing(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta"))
    booted.advance()
    write_unannounced(
        booted, group, "please cut the release", entry_id="wake-removed-1",
        at_ms=booted.daemon.clock.ms(),
        mentions=(booted.agent_id("alpha"), booted.agent_id("beta")),
    )
    booted.advance()
    handle(booted, "conversations.members.remove", conversationId=group,
           memberId=booted.agent_id("beta"), idempotencyKey="drop-beta")

    sweep(booted)

    assert len(group_rows(booted, "alpha", group)) == 1
    assert group_rows(booted, "beta") == []
    assert not woken(booted, "beta")


def test_the_batch_tells_the_agent_it_is_a_group_message(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta"))
    send(booted, group, "please cut the release", key="wake-render-1",
         mentions=[booted.agent_id("alpha")])

    answer = batch(booted, "alpha")

    text = answer["delivery"]["text"]
    (row,) = group_rows(booted, "alpha", group)
    assert [item["messageId"] for item in answer["items"]] == [row.message_id]
    assert f"conversationId: {group}" in text
    assert f"from: {OWNER}" in text
    group_line = next(line for line in text.splitlines() if line.startswith("group: "))
    assert group in group_line
    assert "harness_reply" in group_line
    assert "conversations.history" in group_line
    assert "please cut the release" in text
