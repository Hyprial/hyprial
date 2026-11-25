"""Real isolated daemon + real HTTP process for the ``conversations.*`` reads.

``conversations.list`` / ``conversations.history`` are HTTP exposed with the
``threads.*`` owner-user selector: the host injects ``from=user:<owner>`` and
the browser may not report the daemon/MCP caller identity.  The reads keep
their IPC semantics (same params, result shape, cell/thread/legacy ids), so
this module drives them through the real HTTP entry only.
"""

from __future__ import annotations

from hyprial.kernel import daemon_request_once

from tests.integration.request_http.conftest import Isolation

ACTOR = "user:h01-owner"


def test_conversation_reads_are_served_with_the_owner_user_selector(
    http_server, isolation: Isolation
) -> None:
    status, _, catalog = http_server.request("GET", "/daemon/request")
    assert status == 200
    methods = {item["name"]: item for item in catalog["methods"]}
    for name in ("conversations.list", "conversations.history"):
        assert methods[name]["httpBinding"]["identity"] == {
            "mode": "local-owner-user-selector",
            "param": "from",
        }
        assert methods[name]["httpBinding"].get("readiness") is None
        assert methods[name]["state"] == "available"

    # A fresh daemon has no conversation for the owner, and an unknown cell
    # answers the empty page rather than an error.
    status, _, payload = http_server.request(
        "POST", "/daemon/request/conversations.list", body={}
    )
    assert status == 200, payload
    assert payload["dispatch"] == "dispatched"
    assert payload["method"] == "conversations.list"
    empty = payload["result"]
    assert empty["ok"] is True
    assert empty["participant"] == ACTOR
    assert empty["conversations"] == [] and empty["nextCursor"] is None

    unknown_cell = "c." + "a" * 26
    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/conversations.history",
        body={"conversationId": unknown_cell},
    )
    assert status == 200, payload
    assert payload["result"]["ok"] is True
    assert payload["result"]["conversationId"] == unknown_cell
    assert payload["result"]["entries"] == []
    assert "threadId" not in payload["result"]
    assert payload["result"]["legacyConversationId"] is None

    # The HTTP entry presents the owner as ``from``; the daemon records that
    # owner's conversation, so the list and history read it back.
    created = daemon_request_once(
        "agent.create",
        {"name": "conversation-peer"},
        socket_path=isolation.socket_path,
        timeout=10,
    )
    agent = created.get("agent") if isinstance(created, dict) else None
    peer = agent["uri"] if isinstance(agent, dict) else created["uri"]
    daemon_request_once(
        "message.send",
        {
            "from": ACTOR,
            "to": [peer],
            "message": "conversation body",
            "summary": "conversation body",
            "conversationId": "conv-http",
            "idempotencyKey": "conv-http-1",
        },
        socket_path=isolation.socket_path,
        timeout=10,
    )

    status, _, payload = http_server.request(
        "POST", "/daemon/request/conversations.list", body={}
    )
    assert status == 200, payload
    listed = payload["result"]
    assert listed["participant"] == ACTOR
    head = next(
        row
        for row in listed["conversations"]
        if row["legacyConversationId"] == "conv-http"
    )
    assert head["kind"] == "user" and head["peer"] == peer
    assert "threadId" not in head and head["messages"] == 1
    cell = head["conversationId"]

    status, _, payload = http_server.request(
        "POST",
        "/daemon/request/conversations.history",
        body={"conversationId": cell},
    )
    assert status == 200, payload
    history = payload["result"]
    assert history["conversationId"] == cell
    assert "threadId" not in history
    assert history["legacyConversationId"] == "conv-http"
    assert [entry["message"] for entry in history["entries"]] == [
        "conversation body"
    ]
    assert history["entries"][0]["from"] == ACTOR
    assert history["entries"][0]["to"] == peer

    # Schema refusals happen before dispatch.
    for method, params in (
        ("conversations.list", {"limit": 0}),
        ("conversations.list", {"extra": 1}),
        ("conversations.history", {}),
        ("conversations.history", {"conversationId": "conv-http"}),
    ):
        status, _, body = http_server.request(
            "POST", f"/daemon/request/{method}", body=params
        )
        assert (status, body["code"], body["dispatch"]) == (
            400,
            "INVALID_ARGUMENT",
            "not-dispatched",
        ), (method, params, body)

    # A browser cannot report the daemon-supplied identity: ``from`` (the
    # host-injected owner selector), ``actor`` (its agent alias), and the
    # MCP session credentials are all refused before dispatch.
    forged = {
        "from": "user:someone-else",
        "actor": "agent:h01-owner:h01-node:someone-else",
        "sessionRef": "forged",
        "onBehalfOf": peer,
    }
    for method, required in (
        ("conversations.list", {}),
        ("conversations.history", {"conversationId": cell}),
    ):
        for field, value in forged.items():
            status, _, body = http_server.request(
                "POST",
                f"/daemon/request/{method}",
                body={**required, field: value},
            )
            assert (status, body["code"], body["dispatch"]) == (
                400,
                "INVALID_ARGUMENT",
                "not-dispatched",
            ), (method, field, body)
            assert "daemon session identity is not accepted over HTTP" in body["message"]
