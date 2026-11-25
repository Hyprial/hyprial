"""The owner posts to a group over HTTP with ``conversations.post`` (slice GH).

Two daemons run the same script, one through the IPC ``message.send`` with
the group id (GC3) and one through the HTTP host's ``conversations.post``:
the answers, the group document as ``conversations.history`` reads it and
the rows the members hold must be the same, apart from the wall-clock
``createdAtMs`` the group stamp takes.

Over HTTP the caller is always the owner, who is a member of every group
and can never leave one, so ``CONVERSATION_NOT_MEMBER`` cannot be reached
through the HTTP host; the method's refusal of a non-member is shown with
an agent's session over IPC, which is the same handler.
"""

from __future__ import annotations

import json
from typing import Any

from tests.daemon.conversations.group_delivery.delivery_support import group_rows
from tests.daemon.conversations.group_messages.group_message_support import (
    OWNER,
    handle,
    history,
    make_group,
    refused,
    send,
)
from tests.daemon.conversations.group_requests.group_request_support import (
    GroupDaemon,
    group_daemon,
)
from tests.daemon.conversations.group_surfaces.surface_support import http_call, http_server
from tests.support.golden.app import golden_daemon as golden_daemon

UNKNOWN = "g.unknowngroupaaaaaaaaaaaaaa"
FIRST_KEY = "http-post-first-key-0001"
SECOND_KEY = "http-post-second-key-0002"


def _post(text: str, key: str, group: str, **extra: Any) -> dict[str, Any]:
    return {"conversationId": group, "message": text, "summary": text[:80],
            "idempotencyKey": key, **extra}


def _entries(booted: GroupDaemon, group: str) -> list[dict[str, Any]]:
    entries = history(booted, group, limit=200)["entries"]
    return [{key: value for key, value in entry.items() if key != "createdAtMs"}
            for entry in entries]


def _rows(booted: GroupDaemon, group: str) -> dict[str, list[tuple[Any, ...]]]:
    return {
        name: [(row.message_id, row.conversation_id, row.sender, row.recipient,
                json.loads(row.payload), row.intent, row.summary, row.idempotency_key)
               for row in group_rows(booted, name, group)]
        for name in ("alpha", "beta", "gamma")
    }


def test_an_http_post_writes_and_delivers_as_the_ipc_send_does(golden_daemon,
                                                              monkeypatch) -> None:
    over_ipc = group_daemon(golden_daemon, monkeypatch)
    over_http = group_daemon(golden_daemon, monkeypatch)
    group = make_group(over_ipc)
    assert make_group(over_http) == group
    alpha = over_ipc.agent_id("alpha")
    server = http_server(over_http)

    ipc_first = send(over_ipc, group, "please cut the release", key=FIRST_KEY)
    ipc_second = send(over_ipc, group, "and tag it", key=SECOND_KEY,
                      replyTo=ipc_first["messageId"], mentions=[alpha])
    first_status, first = http_call(
        server, "conversations.post", _post("please cut the release", FIRST_KEY, group))
    second_status, second = http_call(
        server, "conversations.post",
        _post("and tag it", SECOND_KEY, group,
              replyTo=first.get("result", {}).get("messageId"), mentions=[alpha]))

    assert (first_status, second_status) == (200, 200), (first, second)
    assert (first["result"], second["result"]) == (ipc_first, ipc_second)
    assert first["dispatch"] == "dispatched"
    assert _entries(over_http, group) == _entries(over_ipc, group)
    [replied] = [entry for entry in _entries(over_http, group)
                 if entry["messageId"] == second["result"]["messageId"]]
    assert (replied["replyTo"]["messageId"], replied["mentions"]) == (
        first["result"]["messageId"], [alpha],
    )
    assert _rows(over_http, group) == _rows(over_ipc, group)
    assert len(_rows(over_http, group)["alpha"]) == 1
    assert _rows(over_http, group)["beta"] == []


def test_a_replayed_http_post_writes_nothing_more(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    server = http_server(booted)
    body = _post("please cut the release", FIRST_KEY, group)

    _, first = http_call(server, "conversations.post", body)
    status, again = http_call(server, "conversations.post", body)

    assert status == 200, again
    assert again["result"]["deliveries"][0]["duplicate"] is True
    assert again["result"]["messageId"] == first["result"]["messageId"]
    assert len([entry for entry in _entries(booted, group)
                if entry["messageClass"] == "interaction"]) == 1


def test_the_http_post_refusals(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted)
    server = http_server(booted)
    target = {"to": [booted.address("alpha")]}

    unknown = http_call(server, "conversations.post", _post("hello", FIRST_KEY, UNKNOWN))
    direct_id = http_call(server, "conversations.post", _post("hello", FIRST_KEY, "conv-1"))
    with_to = http_call(server, "conversations.post", _post("hello", FIRST_KEY, group, **target))
    stranger = http_call(server, "conversations.post",
                         _post("hello", FIRST_KEY, group, mentions=[booted.agent_id("gamma")]))
    bad_reply = http_call(server, "conversations.post",
                          _post("hello", FIRST_KEY, group, replyTo="not-a-message"))

    assert (unknown[1]["code"], unknown[1]["dispatch"]) == ("CONVERSATION_NOT_FOUND", "dispatched")
    assert (direct_id[0], direct_id[1]["code"], direct_id[1]["dispatch"]) == (
        400, "INVALID_ARGUMENT", "not-dispatched")
    assert (with_to[0], with_to[1]["code"], with_to[1]["dispatch"]) == (
        400, "INVALID_ARGUMENT", "not-dispatched")
    assert (stranger[1]["code"], stranger[1]["dispatch"]) == ("INVALID_ARGUMENT", "dispatched")
    assert (bad_reply[1]["code"], bad_reply[1]["dispatch"]) == ("INVALID_ARGUMENT", "dispatched")
    assert [entry for entry in _entries(booted, group)
            if entry["messageClass"] == "interaction"] == []


def test_a_non_member_is_refused_by_the_post_method(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = make_group(booted, ("alpha",))

    error = refused(booted, "conversations.post", "gamma", **_post("hello", FIRST_KEY, group))

    assert (error.code, error.data) == ("CONVERSATION_NOT_MEMBER", {"conversationId": group})
    assert handle(booted, "conversations.post", "alpha",
                  **_post("hello", FIRST_KEY, group))["actingPrincipal"] == booted.address("alpha")
    assert OWNER in [member["memberId"] for member in
                     handle(booted, "conversations.get", conversationId=group)["members"]]


def test_a_group_screen_needs_only_http_calls(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    server = http_server(booted)
    beta = booted.agent_id("beta")

    calls = [
        ("conversations.create", {"title": "release", "members": ["alpha"],
                                  "idempotencyKey": "screen-create"}),
    ]
    status, created = http_call(server, *calls[0])
    assert status == 200, created
    group = created["result"]["conversationId"]
    calls += [
        ("conversations.members.add", {"conversationId": group, "member": "beta",
                                       "idempotencyKey": "screen-add"}),
        ("conversations.post", _post("please cut the release", FIRST_KEY, group)),
        ("conversations.list", {"kinds": ["direct", "group"]}),
        ("conversations.get", {"conversationId": group}),
        ("conversations.history", {"conversationId": group, "mode": "tail", "limit": 50}),
        ("conversations.members.remove", {"conversationId": group, "memberId": beta,
                                          "idempotencyKey": "screen-remove"}),
    ]
    answers = {method: http_call(server, method, params) for method, params in calls[1:]}

    assert {method: status for method, (status, _) in answers.items()} == {
        method: 200 for method, _ in calls[1:]
    }, answers
    listed = answers["conversations.list"][1]["result"]["conversations"]
    assert [head["conversationId"] for head in listed if head["kind"] == "group"] == [group]
    entries = answers["conversations.history"][1]["result"]["entries"]
    assert entries[-1]["message"] == "please cut the release"
