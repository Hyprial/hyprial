"""``message.reply`` to a group row stays in the group (slice GC3).

A pending row whose conversation is a group (GC4 writes them; here
``deliver_row`` stands in) is answered by a group entry that replies to the
group message the row carries.  Nothing is sent 1:1 to the author; the row
is acknowledged as for any reply.
"""

from __future__ import annotations

from hyprial.kernel import reply_message_id
from tests.daemon.conversations.group_messages.group_message_support import (
    OWNER,
    deliver_row,
    group_daemon,
    handle,
    make_group,
    messages,
    pending_ids,
    reply,
    reply_refused,
    send,
)
from tests.support.golden.app import golden_daemon as golden_daemon

UNKNOWN = "g.unknowngroupaaaaaaaaaaaaaa"


def _asked(booted, group: str) -> str:  # type: ignore[no-untyped-def]
    return str(send(booted, group, "please cut the release", key="ask-1")["messageId"])


def test_a_reply_is_a_group_entry_and_acknowledges_the_row(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    asked = _asked(booted, group)
    deliver_row(booted, group, asked, "alpha", row_id="row-alpha")
    booted.advance()

    answer = reply(booted, "alpha", "row-alpha", "tag is pushed")

    reply_id = reply_message_id("row-alpha")
    assert answer == {
        "ok": True, "messageId": "row-alpha", "replyMessageId": reply_id,
        "replied": True, "acknowledged": True, "queued": False,
    }
    last = messages(booted, group)[-1]
    assert (last["messageId"], last["from"], last["intent"], last["message"]) == (
        reply_id, booted.agent_id("alpha"), "reply", "tag is pushed",
    )
    assert last["replyTo"] == {"messageId": asked, "from": OWNER,
                               "excerpt": "please cut the release"}
    assert "row-alpha" not in pending_ids(booted, "alpha")


def test_a_group_reply_is_never_a_direct_message(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    asked = _asked(booted, group)
    deliver_row(booted, group, asked, "alpha", row_id="row-alpha")

    reply(booted, "alpha", "row-alpha", "tag is pushed")

    reply_id = reply_message_id("row-alpha")
    assert reply_id not in pending_ids(booted, OWNER)
    assert all(row["messageId"] != reply_id for row in booted.daemon.outbox_keys())
    assert booted.daemon.outbox_keys() == []


def test_a_retry_after_the_acknowledgement_answers_the_same_reply(
    golden_daemon, monkeypatch
) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    asked = _asked(booted, group)
    deliver_row(booted, group, asked, "alpha", row_id="row-alpha")
    first = reply(booted, "alpha", "row-alpha", "tag is pushed")
    booted.advance()

    again = reply(booted, "alpha", "row-alpha", "tag is pushed")

    assert (again["replyMessageId"], again["replied"], again["acknowledged"]) == (
        first["replyMessageId"], True, True,
    )
    assert [entry["messageId"] for entry in messages(booted, group)].count(
        first["replyMessageId"]) == 1


def test_two_members_replying_to_one_message_are_two_entries(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    asked = _asked(booted, group)
    deliver_row(booted, group, asked, "alpha", row_id="row-alpha")
    deliver_row(booted, group, asked, "beta", row_id="row-beta")

    reply(booted, "alpha", "row-alpha", "tag is pushed")
    booted.advance()
    reply(booted, "beta", "row-beta", "notes look fine")

    replies = [entry for entry in messages(booted, group) if entry["intent"] == "reply"]
    assert [entry["message"] for entry in replies] == ["tag is pushed", "notes look fine"]
    assert {entry["replyTo"]["messageId"] for entry in replies} == {asked}


def test_a_removed_member_cannot_reply_and_the_row_stays(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    asked = _asked(booted, group)
    deliver_row(booted, group, asked, "beta", row_id="row-beta")
    booted.advance()
    handle(booted, "conversations.members.remove", conversationId=group,
           memberId=booted.agent_id("beta"), idempotencyKey="drop-beta")

    error = reply_refused(booted, "beta", "row-beta", "too late")

    assert error.code == "CONVERSATION_NOT_MEMBER"
    assert "row-beta" in pending_ids(booted, "beta")


def test_a_row_of_an_unknown_group_is_not_found(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    deliver_row(booted, UNKNOWN, "some-message", "alpha", row_id="row-unknown")

    error = reply_refused(booted, "alpha", "row-unknown", "hello?")

    assert error.code == "CONVERSATION_NOT_FOUND"
    assert "row-unknown" in pending_ids(booted, "alpha")


def test_a_row_without_a_group_message_cannot_be_answered(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    deliver_row(booted, group, None, "alpha", row_id="row-bare")
    deliver_row(booted, group, "no-such-entry", "alpha", row_id="row-stale")

    bare = reply_refused(booted, "alpha", "row-bare", "hello?")
    stale = reply_refused(booted, "alpha", "row-stale", "hello?")

    assert (bare.code, stale.code) == ("MESSAGE_REPLY_UNAVAILABLE", "MESSAGE_REPLY_UNAVAILABLE")
    assert {"row-bare", "row-stale"} <= set(pending_ids(booted, "alpha"))
    assert [entry["intent"] for entry in messages(booted, group)] == []


def test_a_group_reply_to_owner_adds_no_mentions(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    asked = _asked(booted, group)
    deliver_row(booted, group, asked, "alpha", row_id="row-alpha")

    reply(booted, "alpha", "row-alpha", "on it")

    last = messages(booted, group)[-1]
    assert not last.get("mentions")


def test_a_group_reply_to_agent_mentions_author_by_default(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta", "gamma"))
    asked = send(booted, group, "beta please check", "alpha", key="ask-agent")["messageId"]
    deliver_row(booted, group, asked, "beta", sender=booted.address("alpha"), row_id="row-beta")

    reply(booted, "beta", "row-beta", "checked")

    last = messages(booted, group)[-1]
    assert last["mentions"] == [booted.agent_id("alpha")]


def test_a_group_reply_no_mention_omits_default_author(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    asked = send(booted, group, "beta please check", "alpha", key="ask-nomention")["messageId"]
    deliver_row(booted, group, asked, "beta", sender=booted.address("alpha"), row_id="row-beta")

    reply(booted, "beta", "row-beta", "quiet ack", noMention=True)

    last = messages(booted, group)[-1]
    assert not last.get("mentions")


def test_a_group_reply_adds_explicit_mentions_and_deduplicates_author(
    golden_daemon, monkeypatch
) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta", "gamma"))
    asked = send(booted, group, "beta please check", "alpha", key="ask-extra")["messageId"]
    deliver_row(booted, group, asked, "beta", sender=booted.address("alpha"), row_id="row-beta")

    reply(
        booted,
        "beta",
        "row-beta",
        "checked with gamma",
        mentions=[booted.agent_id("alpha"), booted.agent_id("gamma")],
    )

    last = messages(booted, group)[-1]
    assert last["mentions"] == [booted.agent_id("alpha"), booted.agent_id("gamma")]


def test_a_group_reply_no_mention_with_explicit_mentions_keeps_explicit_only(
    golden_daemon, monkeypatch
) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta", "gamma"))
    asked = send(booted, group, "beta please check", "alpha", key="ask-override")["messageId"]
    deliver_row(booted, group, asked, "beta", sender=booted.address("alpha"), row_id="row-beta")

    reply(
        booted,
        "beta",
        "row-beta",
        "handing off to gamma",
        noMention=True,
        mentions=[booted.agent_id("gamma")],
    )

    last = messages(booted, group)[-1]
    assert last["mentions"] == [booted.agent_id("gamma")]


def test_a_group_reply_to_self_adds_no_default_mention(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    asked = send(booted, group, "self note", "alpha", key="ask-self")["messageId"]
    deliver_row(booted, group, asked, "alpha", sender=booted.address("alpha"), row_id="row-self")

    reply(booted, "alpha", "row-self", "self reply")

    last = messages(booted, group)[-1]
    assert not last.get("mentions")


def test_a_group_reply_when_author_removed_adds_no_mention(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    asked = send(booted, group, "beta check this", "alpha", key="ask-removed")["messageId"]
    deliver_row(booted, group, asked, "beta", sender=booted.address("alpha"), row_id="row-beta")
    booted.advance()
    handle(
        booted,
        "conversations.members.remove",
        conversationId=group,
        memberId=booted.agent_id("alpha"),
        idempotencyKey="drop-alpha",
    )

    reply(booted, "beta", "row-beta", "alpha left")

    last = messages(booted, group)[-1]
    assert not last.get("mentions")


def test_a_group_reply_with_bad_mentions_refuses_without_acking(
    golden_daemon, monkeypatch
) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    asked = _asked(booted, group)
    deliver_row(booted, group, asked, "alpha", row_id="row-alpha")

    non_member = reply_refused(
        booted, "alpha", "row-alpha", "bad mention", mentions=["a.strangeraaaaaaaaaaaaaaaaaa"]
    )
    dup = reply_refused(
        booted,
        "alpha",
        "row-alpha",
        "dup mention",
        mentions=[booted.agent_id("beta"), booted.agent_id("beta")],
    )
    bad_type = reply_refused(booted, "alpha", "row-alpha", "bad type", mentions="beta")

    assert (non_member.code, dup.code, bad_type.code) == (
        "INVALID_ARGUMENT",
        "INVALID_ARGUMENT",
        "INVALID_ARGUMENT",
    )
    assert "row-alpha" in pending_ids(booted, "alpha")


def test_a_direct_reply_refuses_mentions_and_no_mention(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    sent = handle(
        booted,
        "message.send",
        to=[booted.address("alpha")],
        message="direct ping",
        summary="direct ping",
        idempotencyKey="direct-ping-1",
    )
    booted.advance()

    bad_mentions = reply_refused(
        booted,
        "alpha",
        sent["messageId"],
        "direct reply",
        mentions=[booted.agent_id("beta")],
    )
    bad_no_mention = reply_refused(
        booted,
        "alpha",
        sent["messageId"],
        "direct reply",
        noMention=True,
    )

    assert (bad_mentions.code, bad_no_mention.code) == ("INVALID_ARGUMENT", "INVALID_ARGUMENT")
    assert sent["messageId"] in pending_ids(booted, "alpha")
