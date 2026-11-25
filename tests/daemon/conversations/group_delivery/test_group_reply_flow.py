"""A delivered group row answers in the group, and agent chains are not capped (slice GC4).

GC3's reply contract end to end: deliver -> ``message.reply`` -> a group
entry replying to the group message, the row acknowledged.  Owner decision
§10 8.5 rejected the hop cap: two agents replying to each other with
explicit ``mentions`` (2026-11-21 ruling) keep waking each other.
§10 8.4 originally kept an unlinked member's row out of the batch through
link dispatch admission; T1b removed that gate, so an agent-authored row
reaches every mentioned member's batch (the no-link test below records the
change).
"""

from __future__ import annotations

import json

from hyprial.kernel import reply_message_id
from tests.daemon.conversations.group_delivery.delivery_support import (
    OWNER,
    batch,
    group_daemon,
    group_rows,
    linked_group_daemon,
    make_group,
    messages,
    reply,
    send,
)
from tests.support.golden.app import SESSIONS, golden_daemon as golden_daemon


def _ack(booted, who: str, row_id: str) -> None:  # type: ignore[no-untyped-def]
    booted.app.handle(
        "message.ack",
        {"from": booted.address(who), "sessionRef": SESSIONS[who], "messageId": row_id},
        _trusted_message_origin="ipc",
    )


def test_deliver_then_reply_writes_a_group_entry_and_acks_the_row(
    golden_daemon, monkeypatch
) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta"))
    asked = send(booted, group, "please cut the release", key="flow-ask-1",
                 mentions=[booted.agent_id("alpha"), booted.agent_id("beta")])
    (row,) = group_rows(booted, "alpha", group)
    booted.advance()

    answer = reply(booted, "alpha", row.message_id, "tag is pushed")

    assert (answer["replied"], answer["acknowledged"]) == (True, True)
    assert answer["replyMessageId"] == reply_message_id(row.message_id)
    last = messages(booted, group)[-1]
    assert (last["messageId"], last["from"], last["message"]) == (
        reply_message_id(row.message_id), booted.agent_id("alpha"), "tag is pushed")
    assert last["replyTo"]["messageId"] == asked["messageId"]
    assert group_rows(booted, "alpha") == []
    assert len(group_rows(booted, "beta", group)) == 1
    assert list(booted.app._inbox.pending_messages(OWNER)) == []


def test_two_agents_replying_to_each_other_is_not_capped(golden_daemon, monkeypatch) -> None:
    """Owner decision §10 8.5: no hop cap; every mentioned reply in the chain wakes the peer."""

    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta"))
    send(booted, group, "beta, ping", "alpha", key="flow-chain-0",
         mentions=[booted.agent_id("beta")])
    speaker, listener = "beta", "alpha"
    for turn in range(8):
        booted.advance()
        (row,) = group_rows(booted, speaker, group)
        group_msg_id = json.loads(row.payload)["groupMessageId"]
        send(booted, group, f"turn {turn}", speaker, key=f"flow-chain-{turn + 1}",
             replyTo=group_msg_id, mentions=[booted.agent_id(listener)])
        _ack(booted, speaker, row.message_id)
        assert group_rows(booted, speaker) == [], turn
        assert len(group_rows(booted, listener, group)) == 1, turn
        speaker, listener = listener, speaker

    assert len(messages(booted, group)) == 9



def test_an_agent_mention_without_a_link_enters_the_batch(
    golden_daemon, monkeypatch
) -> None:
    """T1b: nothing asks a link gate, so a mentioned member's row is batched."""

    booted = linked_group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta"))

    sent = send(booted, group, "beta, check the notes", "alpha", key="flow-nolink-1",
                mentions=[booted.agent_id("beta")])

    assert sent["deliveries"][0]["accepted"] is True
    (row,) = group_rows(booted, "beta", group)
    assert [item["messageId"] for item in batch(booted, "beta")["items"]] == [row.message_id]
    assert [entry["message"] for entry in messages(booted, group)] == ["beta, check the notes"]


def test_an_agent_mention_is_delivered_and_answered(
    golden_daemon, monkeypatch
) -> None:
    booted = linked_group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta"))

    send(booted, group, "beta, check the notes", "alpha", key="flow-link-1",
         mentions=[booted.agent_id("beta")])

    (row,) = group_rows(booted, "beta", group)
    assert [item["messageId"] for item in batch(booted, "beta")["items"]] == [row.message_id]
    answer = reply(booted, "beta", row.message_id, "notes look fine")
    assert (answer["replied"], answer["acknowledged"]) == (True, True)
    assert group_rows(booted, "beta") == []
    assert messages(booted, group)[-1]["message"] == "notes look fine"


def test_two_agents_replying_to_each_other_see_every_row(golden_daemon, monkeypatch) -> None:
    """Each mentioned reply reaches the peer's batch (no cap)."""

    booted = linked_group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta"))
    send(booted, group, "beta, ping", "alpha", key="flow-linked-chain-0",
         mentions=[booted.agent_id("beta")])
    speaker, listener = "beta", "alpha"
    for turn in range(4):
        booted.advance()
        (row,) = group_rows(booted, speaker, group)
        assert [item["messageId"] for item in batch(booted, speaker)["items"]] == [
            row.message_id], turn
        group_msg_id = json.loads(row.payload)["groupMessageId"]
        send(booted, group, f"turn {turn}", speaker, key=f"flow-linked-chain-{turn + 1}",
             replyTo=group_msg_id, mentions=[booted.agent_id(listener)])
        _ack(booted, speaker, row.message_id)
        speaker, listener = listener, speaker


def test_reply_to_agent_wakes_author_by_default_and_no_mention_suppresses_it(
    golden_daemon, monkeypatch
) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta", "gamma"))
    asked = send(booted, group, "beta, check this", "alpha", key="reply-wake-0",
                 mentions=[booted.agent_id("beta")])
    (row_b,) = group_rows(booted, "beta", group)
    booted.advance()

    first_reply = reply(booted, "beta", row_b.message_id, "done checking")

    assert group_rows(booted, "beta") == []
    assert group_rows(booted, "gamma") == []
    assert list(booted.app._inbox.pending_messages(OWNER)) == []
    (row_a,) = group_rows(booted, "alpha", group)
    payload = json.loads(row_a.payload)
    assert payload["groupMessageId"] == first_reply["replyMessageId"]
    assert payload["mentions"] == [booted.agent_id("alpha")]
    assert payload["replyTo"]["messageId"] == asked["messageId"]

    booted.advance()
    reply(booted, "alpha", row_a.message_id, "thanks", noMention=True)

    for name in ("alpha", "beta", "gamma"):
        assert group_rows(booted, name) == [], name


def test_two_agents_replying_via_message_reply_keep_waking_each_other(
    golden_daemon, monkeypatch
) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta"))
    send(booted, group, "beta, ping", "alpha", key="reply-loop-0",
         mentions=[booted.agent_id("beta")])
    speaker, listener = "beta", "alpha"
    for turn in range(6):
        booted.advance()
        (row,) = group_rows(booted, speaker, group)
        reply(booted, speaker, row.message_id, f"turn {turn}")
        assert group_rows(booted, speaker) == [], turn
        assert len(group_rows(booted, listener, group)) == 1, turn
        speaker, listener = listener, speaker

    assert len(messages(booted, group)) == 7


def test_reply_with_extra_mentions_wakes_author_and_added_member(
    golden_daemon, monkeypatch
) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta", "gamma"))
    send(booted, group, "beta, ping", "alpha", key="reply-extra-0",
         mentions=[booted.agent_id("beta")])
    (row_b,) = group_rows(booted, "beta", group)
    booted.advance()

    answer = reply(booted, "beta", row_b.message_id, "looping in gamma",
                   mentions=[booted.agent_id("gamma")])

    for target in ("alpha", "gamma"):
        (row,) = group_rows(booted, target, group)
        payload = json.loads(row.payload)
        assert payload["groupMessageId"] == answer["replyMessageId"]
        assert payload["mentions"] == [booted.agent_id("alpha"), booted.agent_id("gamma")]
    assert group_rows(booted, "beta") == []
