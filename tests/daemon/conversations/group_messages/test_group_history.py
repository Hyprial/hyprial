"""``conversations.history`` for a group: the document, members only (slice GC3).

The page engine, modes and cursors are direct history's; the rows are the
group document's entries in ``(createdAtMs, entryId)`` order, from every
writer, system entries included.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from hyprial.daemon.impl.application.messaging.inbox_surface.groups.requests import group_log
from hyprial.daemon.impl.composition.conversations.placement import OwnerHomeGroupPlacement
from hyprial.daemon.impl.conversations.groups import CLASS_INTERACTION, GroupEntry, GroupLog
from tests.daemon.conversations.group_messages.group_message_support import (
    OWNER,
    group_daemon,
    handle,
    history,
    make_group,
    refused,
    send,
)
from tests.support.golden.app import golden_daemon as golden_daemon

UNKNOWN = "g.unknowngroupaaaaaaaaaaaaaa"


def _other_writer(booted, group: str, entry_id: str, text: str, at: int) -> None:  # type: ignore[no-untyped-def]
    """An entry written by this owner's other machine (another writer)."""

    own = group_log(booted.app)
    log = GroupLog(placement=own._placement, writer="node-2")
    assert isinstance(own._placement, OwnerHomeGroupPlacement)
    body = text.encode()
    log.append(group, [GroupEntry(
        entry_id=entry_id, conversation_id=group, message_class=CLASS_INTERACTION,
        sender=booted.agent_id("beta"), created_at_ms=at, recorded_at_ms=at, writer="node-2",
        intent="request", summary=text, body=body,
        payload_sha256=hashlib.sha256(body).hexdigest(),
    )])


def _texts(page: dict[str, Any]) -> list[str]:
    return [entry["message"] for entry in page["entries"]]


def test_a_group_page_has_the_published_entry_shape(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    beta = booted.agent_id("beta")
    first = send(booted, group, "please cut the release", key="h-1")
    booted.advance()
    second = send(booted, group, "done", "alpha", key="h-2", replyTo=first["messageId"],
                  mentions=[beta])

    page = history(booted, group)

    assert (page["conversationId"], page["legacyConversationId"]) == (
        group, group,
    )
    assert "threadId" not in page
    assert page["recordAvailable"] is True
    assert page["nextCursor"] is None
    assert page["memberNames"] == {
        OWNER: "op",
        booted.agent_id("alpha"): "alpha",
        beta: "beta",
    }
    created, added_alpha, added_beta, owner_message, alpha_message = page["entries"]
    assert alpha_message == {
        "kind": "message",
        "messageId": second["messageId"],
        "conversationId": group,
        "from": booted.agent_id("alpha"),
        "to": group,
        "intent": "reply",
        "createdAtMs": alpha_message["createdAtMs"],
        "message": "done",
        "origin": {},
        "messageClass": "interaction",
        "replyTo": {"messageId": first["messageId"], "from": OWNER,
                    "excerpt": "please cut the release"},
        "mentions": [beta],
    }
    assert (owner_message["from"], owner_message["intent"]) == (OWNER, "request")
    assert "replyTo" not in owner_message and "mentions" not in owner_message
    assert owner_message["createdAtMs"] < alpha_message["createdAtMs"]
    assert [entry["messageClass"] for entry in (created, added_alpha, added_beta)] == [
        "system", "system", "system",
    ]
    assert created["system"] == {"event": "created", "memberId": OWNER}
    assert added_beta["system"] == {"event": "member-added", "memberId": beta}
    assert created["from"] == OWNER and created["to"] == group
    assert created["message"] and "release" in created["message"]
    assert "beta" in added_beta["message"]


def test_paging_orders_entries_of_two_authors_and_two_writers(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    send(booted, group, "one", key="p-1")
    booted.advance()
    send(booted, group, "two", "alpha", key="p-2")
    booted.advance(100)
    send(booted, group, "four", key="p-4")
    four_at = history(booted, group)["entries"][-1]["createdAtMs"]
    _other_writer(booted, group, "00000000-0000-5000-8000-00000000003a", "three", four_at - 50)

    texts: list[str] = []
    cursor = None
    while True:
        page = history(booted, group, limit=2, **({"cursor": cursor} if cursor else {}))
        texts.extend(_texts(page))
        cursor = page["nextCursor"]
        if cursor is None:
            break

    whole = history(booted, group)["entries"]
    assert texts == [entry["message"] for entry in whole]
    assert [entry["message"] for entry in whole if entry["messageClass"] == "interaction"] == [
        "one", "two", "three", "four",
    ]
    order = [(entry["createdAtMs"], entry["messageId"]) for entry in whole]
    assert order == sorted(order)


def test_tail_older_and_after_modes_page_the_group(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha",))
    for index in range(4):
        booted.advance()
        send(booted, group, f"m{index}", key=f"mode-{index}")

    tail = history(booted, group, mode="tail", limit=2)
    older = history(booted, group, mode="older", cursor=tail["nextOlderCursor"], limit=2)
    booted.advance()
    send(booted, group, "m4", "alpha", key="mode-4")
    after = history(booted, group, mode="after", cursor=tail["headCursor"], limit=5)

    assert _texts(tail) == ["m2", "m3"]
    assert _texts(older) == ["m0", "m1"]
    assert _texts(after) == ["m4"]


def test_a_member_agent_reads_and_a_non_member_is_refused(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha",))
    send(booted, group, "hello", key="r-1")

    read = history(booted, group, "alpha")
    error = refused(booted, "conversations.history", "gamma", conversationId=group)

    assert _texts(read)[-1] == "hello"
    assert (error.code, error.data) == ("CONVERSATION_NOT_MEMBER", {"conversationId": group})


def test_a_removed_member_can_no_longer_read(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    booted.advance()
    handle(booted, "conversations.members.remove", conversationId=group,
           memberId=booted.agent_id("beta"), idempotencyKey="drop-beta")

    error = refused(booted, "conversations.history", "beta", conversationId=group)

    assert error.code == "CONVERSATION_NOT_MEMBER"
    assert history(booted, group)["entries"][-1]["system"]["event"] == "member-removed"


def test_an_unknown_group_is_not_found(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)

    error = refused(booted, "conversations.history", conversationId=UNKNOWN)

    assert (error.code, error.data) == ("CONVERSATION_NOT_FOUND", {"conversationId": UNKNOWN})


def test_an_unreadable_document_is_unavailable(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    booted.fs.down = True

    error = refused(booted, "conversations.history", conversationId=group)

    booted.fs.down = False
    assert error.code == "DAEMON_UNAVAILABLE"


def test_a_cursor_is_bound_to_its_reader(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    cursor = history(booted, group, "alpha", limit=1)["nextCursor"]

    error = refused(booted, "conversations.history", conversationId=group, cursor=cursor)

    assert error.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("bad", ["g.short", "x.groupexampleaaaaaaaaaaaaaa"])
def test_a_malformed_id_is_refused(golden_daemon, monkeypatch, bad) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)

    error = refused(booted, "conversations.history", conversationId=bad)

    assert error.code == "INVALID_ARGUMENT"


def test_the_owner_acting_as_this_machines_user_agent_reads_as_the_owner(
    golden_daemon, monkeypatch
) -> None:
    """UA4: the user agent's own id-form address is recognised by its registry marker.

    Each machine's user agent has its own registry-minted id, so the pure
    member-id fold cannot map it to ``user:op``; the group reads canonicalise
    it with this machine's registry first.
    """

    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    send(booted, group, "hello", key="ua-1")
    own = str(booted.user_agent().uri)

    page = history(booted, group, onBehalfOf=own)
    listed = handle(booted, "conversations.list", kinds=["group"], onBehalfOf=own)

    assert "hello" in _texts(page)
    assert group in {head["conversationId"] for head in listed["conversations"]}


def test_group_history_includes_former_members_in_member_names(
    golden_daemon, monkeypatch
) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha", "beta"))
    alpha = booted.agent_id("alpha")
    beta = booted.agent_id("beta")
    booted.advance()
    beta_msg = send(booted, group, "beta message", "beta", key="former-1")
    booted.advance()
    send(
        booted, group, "ack beta", key="former-2",
        replyTo=beta_msg["messageId"], mentions=[beta],
    )
    booted.advance()
    handle(
        booted, "conversations.members.remove",
        conversationId=group, memberId=beta, idempotencyKey="former-drop-beta",
    )
    booted.advance()
    handle(
        booted, "conversations.members.remove", "alpha",
        conversationId=group, memberId=alpha, idempotencyKey="former-leave-alpha",
    )

    tail = history(booted, group, mode="tail", limit=2)
    full = history(booted, group, limit=200)

    expected_names = {OWNER: "op", alpha: "alpha", beta: "beta"}
    assert tail["memberNames"] == expected_names
    assert full["memberNames"] == expected_names
    assert [entry["system"] for entry in tail["entries"]] == [
        {"event": "member-removed", "memberId": beta},
        {"event": "member-left", "memberId": alpha},
    ]
    for entry in full["entries"]:
        assert entry["from"] in full["memberNames"]
        if "replyTo" in entry:
            assert entry["replyTo"]["from"] in full["memberNames"]
        for member_id in entry.get("mentions", ()):
            assert member_id in full["memberNames"]
        if "system" in entry:
            assert entry["system"]["memberId"] in full["memberNames"]


def test_direct_history_omits_member_names(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    make_group(booted, ("alpha",))
    handle(
        booted, "message.send", to=[booted.address("alpha")],
        message="direct hello", summary="direct hello", idempotencyKey="direct-no-names",
    )
    listed = handle(booted, "conversations.list")
    direct_id = listed["conversations"][0]["conversationId"]

    page = handle(booted, "conversations.history", conversationId=direct_id)

    assert "memberNames" not in page
