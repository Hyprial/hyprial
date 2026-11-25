"""``message.send`` to a group: one document entry, members only (slice GC3).

A send with a group ``conversationId`` and no ``to`` appends ONE entry to
the group document, whoever the members are.  The send queues nothing
itself; delivery to the woken members (slice GC4) is tested in
``group_delivery``.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

import pytest

from hyprial.daemon.impl.conversations.groups import GroupLog, fold_group
from tests.daemon.conversations.group_messages.group_message_support import (
    OWNER,
    group_daemon,
    handle,
    history,
    make_group,
    messages,
    pending_ids,
    refused,
    send,
    send_params,
)
from tests.support.golden.app import golden_daemon as golden_daemon

UNKNOWN = "g.unknowngroupaaaaaaaaaaaaaa"


def _document(booted, group: str):  # type: ignore[no-untyped-def]
    from hyprial.daemon.impl.application.messaging.inbox_surface.groups.requests import group_log

    log: GroupLog = group_log(booted.app)
    return log.read(group)


def _interaction(booted, group: str) -> list:  # type: ignore[no-untyped-def]
    return [entry for entry in _document(booted, group) if entry.system is None]


def _nothing_written_but_a_send_works(booted, group: str) -> None:  # type: ignore[no-untyped-def]
    """The refusal wrote nothing, and a well-formed owner send to the group succeeds."""

    assert _interaction(booted, group) == []
    send(booted, group, "control", key="control-send")
    assert [entry.body for entry in _interaction(booted, group)] == [b"control"]


def test_the_owner_sends_one_entry_to_the_group(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)

    answer = send(booted, group, "please cut the release", key="owner-send-1")

    message_id = str(uuid5(NAMESPACE_URL, f"hyprial:send:owner-send-1:{group}:0"))
    assert answer == {
        "ok": True,
        "actingPrincipal": OWNER,
        "operationId": "owner-send-1",
        "conversationId": group,
        "messageId": message_id,
        "deliveries": [{"target": group, "messageId": message_id, "conversationId": group,
                        "accepted": True, "queued": False}],
    }
    [entry] = _interaction(booted, group)
    assert (entry.entry_id, entry.sender, entry.intent) == (message_id, OWNER, "request")
    assert (entry.body, entry.summary, entry.from_address) == (
        b"please cut the release", "please cut the release", OWNER,
    )
    assert (entry.reply_to, entry.mentions) == (None, ())


def test_a_member_agent_sends_under_its_member_id(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)

    answer = send(booted, group, "tag is pushed", "alpha", key="alpha-send-1")

    [entry] = _interaction(booted, group)
    assert answer["actingPrincipal"] == booted.address("alpha")
    assert (entry.sender, entry.from_address) == (booted.agent_id("alpha"),
                                                   booted.address("alpha"))


def test_one_entry_whatever_the_number_of_members(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta", "gamma"))

    send(booted, group, "hello all", key="fanout-1")

    assert len(_interaction(booted, group)) == 1


def test_a_group_send_queues_nothing_but_the_woken_members_rows(
    golden_daemon, monkeypatch
) -> None:
    """No outbox row and no owner row; only explicitly mentioned agent members get rows."""

    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)

    send(booted, group, "fyi", key="quiet-0")
    send(booted, group, "please cut the release", key="quiet-1",
         mentions=[booted.agent_id("alpha"), booted.agent_id("beta")])
    send(booted, group, "on it", "alpha", key="quiet-2", mentions=[booted.agent_id("beta")])

    assert len(pending_ids(booted, "alpha")) == 1
    assert len(pending_ids(booted, "beta")) == 2
    assert pending_ids(booted, OWNER) == []
    assert booted.daemon.outbox_keys() == []
    assert [entry["message"] for entry in messages(booted, group, "beta")] == [
        "fyi", "please cut the release", "on it",
    ]


def test_a_non_member_is_refused_and_nothing_is_written(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha",))

    error = refused(booted, "message.send", "gamma",
                    **send_params(group, "let me in", "gamma-send"))

    assert (error.code, error.data) == ("CONVERSATION_NOT_MEMBER", {"conversationId": group})
    _nothing_written_but_a_send_works(booted, group)


def test_a_removed_member_can_no_longer_send(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    booted.advance()
    handle(booted, "conversations.members.remove", conversationId=group,
           memberId=booted.agent_id("beta"), idempotencyKey="drop-beta")

    error = refused(booted, "message.send", "beta", **send_params(group, "still here?", "b-1"))

    assert error.code == "CONVERSATION_NOT_MEMBER"


def test_an_unknown_group_is_not_found(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)

    error = refused(booted, "message.send", **send_params(UNKNOWN, "hello", "unknown-1"))

    assert (error.code, error.data) == ("CONVERSATION_NOT_FOUND", {"conversationId": UNKNOWN})


def test_a_replay_with_the_same_key_writes_one_entry(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)

    first = send(booted, group, "please cut the release", key="replay-1")
    booted.advance()
    again = send(booted, group, "please cut the release", key="replay-1")

    assert again["messageId"] == first["messageId"]
    assert again["deliveries"] == [{**first["deliveries"][0], "duplicate": True}]
    assert len(_interaction(booted, group)) == 1


def test_another_sender_cannot_reuse_a_key(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    send(booted, group, "please cut the release", key="shared-key")

    error = refused(booted, "message.send", "alpha",
                    **send_params(group, "mine", "shared-key"))

    assert error.code == "INVALID_ARGUMENT"
    assert len(_interaction(booted, group)) == 1


def test_mentions_name_current_members(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    beta = booted.agent_id("beta")

    send(booted, group, "beta, check the notes", key="mention-1", mentions=[beta, OWNER])

    [entry] = _interaction(booted, group)
    assert entry.mentions == (beta, OWNER)


@pytest.mark.parametrize("mention", ["gamma-id", "gamma-name", "unknown", "empty", "twice"])
def test_a_mention_of_a_non_member_is_refused(golden_daemon, monkeypatch, mention) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    beta = booted.agent_id("beta")
    mentions = {
        "gamma-id": [booted.agent_id("gamma")],
        "gamma-name": ["gamma"],
        "unknown": ["a.nobodyaaaaaaaaaaaaaaaaaaaa"],
        "empty": [""],
        "twice": [beta, beta],
    }[mention]

    error = refused(booted, "message.send",
                    **send_params(group, "hello", f"mention-{mention}", mentions=mentions))

    assert error.code == "INVALID_ARGUMENT"
    _nothing_written_but_a_send_works(booted, group)


def test_a_reply_to_names_an_entry_of_this_group(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    text = "please cut the release " + "x" * 200
    first = send(booted, group, text, key="reply-base")
    booted.advance()

    answer = send(booted, group, "tag is pushed", "alpha", key="reply-1",
                  replyTo=first["messageId"])

    entry = next(item for item in _interaction(booted, group)
                 if item.entry_id == answer["messageId"])
    assert entry.intent == "reply"
    assert entry.reply_to is not None
    assert entry.reply_to.to_json() == {
        "messageId": first["messageId"], "from": OWNER, "excerpt": text[:120],
    }


@pytest.mark.parametrize("elsewhere", ["other-group", "direct", "system", "unknown"])
def test_a_reply_to_outside_this_group_is_refused(golden_daemon, monkeypatch, elsewhere) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    other = make_group(booted, ("alpha",), key="other-group", title="other")
    if elsewhere == "other-group":
        target = send(booted, other, "elsewhere", key="other-1")["messageId"]
    elif elsewhere == "direct":
        target = handle(booted, "message.send", to=[booted.address("alpha")],
                        message="direct", summary="direct",
                        idempotencyKey="direct-1")["messageId"]
    elif elsewhere == "system":
        target = _document(booted, group)[0].entry_id
    else:
        target = "no-such-message"

    error = refused(booted, "message.send", "alpha",
                    **send_params(group, "re", f"re-{elsewhere}", replyTo=target))

    assert error.code == "INVALID_ARGUMENT"
    _nothing_written_but_a_send_works(booted, group)


@pytest.mark.parametrize("to", [["agent"], "text"])
def test_to_together_with_a_group_id_is_refused(golden_daemon, monkeypatch, to) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    targets = [booted.address("alpha")] if to == ["agent"] else to

    error = refused(booted, "message.send", **send_params(group, "hi", "to-1", to=targets))

    assert error.code == "INVALID_ARGUMENT"
    _nothing_written_but_a_send_works(booted, group)


def test_an_empty_to_with_a_group_id_is_a_group_send(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)

    send(booted, group, "hi", key="empty-to", to=[])

    assert len(_interaction(booted, group)) == 1


@pytest.mark.parametrize("field", ["resourcePaths", "role", "topic"])
def test_direct_only_fields_are_refused_with_a_group_id(
    golden_daemon, monkeypatch, field
) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    value = {"resourcePaths": ["/tmp/a.txt"], "role": "worker", "topic": "t"}[field]

    error = refused(booted, "message.send", **send_params(group, "hi", "f-1", **{field: value}))

    assert error.code == "INVALID_ARGUMENT"
    _nothing_written_but_a_send_works(booted, group)


def test_mentions_without_a_group_are_refused(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)

    error = refused(booted, "message.send", to=[booted.address("alpha")], message="hi",
                    summary="hi", idempotencyKey="direct-mention",
                    mentions=[booted.agent_id("alpha")])

    assert error.code == "INVALID_ARGUMENT"
    assert pending_ids(booted, "alpha") == []


def test_on_behalf_of_keeps_the_owner_as_author(golden_daemon, monkeypatch) -> None:
    """The stored sender is the caller's sender, as for a direct send."""

    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)

    answer = send(booted, group, "for alpha", key="behalf-1", onBehalfOf=booted.address("alpha"))

    [entry] = _interaction(booted, group)
    assert (answer["actingPrincipal"], entry.sender) == (OWNER, OWNER)


def test_an_unwritable_document_is_unavailable(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    booted.fs.down = True

    error = refused(booted, "message.send", **send_params(group, "hi", "down-1"))

    booted.fs.down = False
    assert error.code == "DAEMON_UNAVAILABLE"
    _nothing_written_but_a_send_works(booted, group)


def test_members_and_title_are_untouched_by_messages(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    before = fold_group(_document(booted, group))

    send(booted, group, "hi", key="state-1")

    assert fold_group(_document(booted, group)) == before
    assert history(booted, group)["entries"][-1]["message"] == "hi"
