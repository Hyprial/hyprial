"""``conversations.list`` with ``kinds`` (slice GC2).

Without ``kinds``, or with ``["direct"]``, the answer is byte for byte the
answer of a daemon without groups, cursors included.  With ``group`` the
caller's groups are listed newest first among the direct conversations, and
a cursor only pages the ``kinds`` it was issued for.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonRequestError

from tests.daemon.conversations.group_requests.group_request_support import (
    OWNER,
    group_daemon,
)
from tests.support.golden.app import SESSIONS, golden_daemon as golden_daemon

BOTH = ["direct", "group"]


def _send(booted, sender: str, to: str, key: str) -> None:  # type: ignore[no-untyped-def]
    answer = booted.app.handle(
        "message.send",
        {
            "from": booted.address(sender),
            "sessionRef": SESSIONS[sender],
            "to": [booted.address(to)],
            "summary": key,
            "message": f"body {key}",
            "conversationId": f"conv-{key}",
            "idempotencyKey": f"key-{key}",
        },
    )
    assert answer["ok"] is True, answer


def _bytes(answer: dict[str, Any]) -> str:
    return json.dumps(answer, sort_keys=False, separators=(",", ":"))


def test_the_default_list_is_unchanged_by_groups(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    _send(booted, "alpha", "beta", "one")
    booted.advance()
    _send(booted, "alpha", "gamma", "two")
    booted.advance()
    before = {
        "page": booted.listed("alpha"),
        "first": booted.listed("alpha", limit=1),
    }
    before["second"] = booted.listed("alpha", limit=1, cursor=before["first"]["nextCursor"])

    booted.create(["alpha", "beta"], key="g1")
    booted.advance()

    for kinds in (None, ["direct"]):
        extra = {} if kinds is None else {"kinds": kinds}
        page = booted.listed("alpha", **extra)
        first = booted.listed("alpha", limit=1, **extra)
        second = booted.listed("alpha", limit=1, cursor=first["nextCursor"], **extra)
        assert _bytes(page) == _bytes(before["page"]), kinds
        assert _bytes(first) == _bytes(before["first"]), kinds
        assert _bytes(second) == _bytes(before["second"]), kinds
        assert {head["kind"] for head in page["conversations"]} == {"agent"}


def test_groups_are_listed_newest_first_among_direct_conversations(
    golden_daemon, monkeypatch
) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    older = booted.create(["alpha"], key="g-old", title="old")["conversationId"]
    booted.advance()
    _send(booted, "alpha", "beta", "between")
    booted.advance()
    newer = booted.create(["alpha", "beta"], key="g-new", title="new")["conversationId"]

    both = booted.listed("alpha", kinds=BOTH)["conversations"]
    only = booted.listed("alpha", kinds=["group"])["conversations"]

    assert [head["kind"] for head in both] == ["group", "agent", "group"]
    assert [both[0]["conversationId"], both[2]["conversationId"]] == [newer, older]
    assert [head["conversationId"] for head in only] == [newer, older]
    times = [head["lastMessageAtMs"] for head in both]
    assert times == sorted(times, reverse=True)


def test_a_new_group_head_has_no_message_yet(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    created = booted.create(["alpha", "beta"], key="g1")

    [head] = booted.listed(kinds=["group"])["conversations"]

    newest_join = max(member["joinedAtMs"] for member in created["members"])
    assert head == {
        "kind": "group",
        "conversationId": created["conversationId"],
        "legacyConversationId": created["conversationId"],
        "title": "release",
        "memberCount": 3,
        "lastMessageAtMs": newest_join,
        "lastMessageId": None,
        "messages": 0,
        "messageClass": "interaction",
    }


def test_a_group_head_is_an_interaction_after_membership_changes(
    golden_daemon, monkeypatch
) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = booted.create(["alpha"], key="g1")["conversationId"]
    booted.advance()
    booted.call("conversations.members.add", conversationId=group, member="beta",
                idempotencyKey="add-beta")

    [head] = booted.listed(kinds=["group"])["conversations"]

    assert (head["messages"], head["messageClass"]) == (0, "interaction")


def test_only_current_members_see_a_group(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = booted.create(["alpha", "beta"], key="g1")["conversationId"]
    booted.advance()
    booted.call("conversations.members.remove", conversationId=group,
                memberId=booted.agent_id("beta"), idempotencyKey="drop-beta")

    def groups(who: str) -> list[str]:
        listed = booted.listed(who, kinds=["group"])["conversations"]
        return [head["conversationId"] for head in listed]

    assert groups(OWNER) == [group]
    assert groups("alpha") == [group]
    assert groups("beta") == []
    assert groups("gamma") == []
    [head] = booted.listed(kinds=["group"])["conversations"]
    assert head["memberCount"] == 2


def test_the_cursor_binds_kinds(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    booted.create(["alpha"], key="g1")
    booted.advance()
    booted.create(["alpha"], key="g2")
    booted.advance()
    _send(booted, "alpha", "beta", "one")
    booted.advance()
    _send(booted, "alpha", "gamma", "two")

    first = booted.listed("alpha", kinds=["group"], limit=1)
    groups_cursor = first["nextCursor"]
    second = booted.listed("alpha", kinds=["group"], limit=1, cursor=groups_cursor)
    direct_cursor = booted.listed("alpha", limit=1)["nextCursor"]
    both = booted.listed("alpha", kinds=["direct", "group"], limit=1)
    reordered = booted.listed("alpha", kinds=["group", "direct"], limit=1,
                              cursor=both["nextCursor"])

    assert groups_cursor is not None and direct_cursor is not None
    assert second["conversations"][0]["conversationId"] != first["conversations"][0]["conversationId"]
    assert reordered["conversations"][0]["conversationId"] != both["conversations"][0]["conversationId"]
    for kinds, cursor in (
        (None, groups_cursor), (["direct"], groups_cursor), (BOTH, groups_cursor),
        (["group"], direct_cursor), (BOTH, direct_cursor), (None, both["nextCursor"]),
    ):
        extra = {} if kinds is None else {"kinds": kinds}
        with pytest.raises(DaemonRequestError) as raised:
            booted.listed("alpha", limit=1, cursor=cursor, **extra)
        assert raised.value.code == "INVALID_ARGUMENT", kinds


def test_an_unreadable_document_lists_no_groups(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    booted.create(["alpha"], key="g1")
    booted.fs.down = True

    answer = booted.listed(kinds=["group"])

    assert (answer["conversations"], answer["recordAvailable"]) == ([], False)
