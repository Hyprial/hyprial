"""conversations.* over id-form agents (release UUID addressing).

The cell-model goldens pin name-form minting (their recordings predate
release #2546).  This runs the same golden daemon with release's id-form
default: the agents are addressed at their ``a.`` id URIs, a session send
between two of them is stamped on their thread, and the conversation reads
find it by those addresses.
"""

from __future__ import annotations

import json
from typing import Any

from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
from hyprial.kernel import canonical_agent_uri, is_agent_id_segment, parse_agent_uri
from tests.support.golden.app import SESSIONS, golden_daemon as golden_daemon


def _address(app: Any, name: str) -> str:
    uri = app.agents.require(name).uri
    parsed = parse_agent_uri(uri)
    assert parsed is not None and is_agent_id_segment(parsed[2]), uri
    return uri


def test_reads_find_a_message_by_the_agents_id_addresses(golden_daemon) -> None:
    app = golden_daemon(id_form=True).app
    alpha, beta = _address(app, "alpha"), _address(app, "beta")
    sent = app.handle(
        "message.send",
        {
            "from": alpha,
            "sessionRef": SESSIONS["alpha"],
            "to": [beta],
            "summary": "hello",
            "message": "hello beta",
            "conversationId": "conv-id-form",
            "idempotencyKey": "key-id-form",
        },
    )
    assert sent["ok"] is True, sent
    reader = {"from": beta, "sessionRef": SESSIONS["beta"]}

    listed = app.handle("conversations.list", reader)
    assert listed["participant"] == beta
    [head] = listed["conversations"]
    assert "threadId" not in head
    assert head["peer"] == alpha
    assert head["legacyConversationId"] == "conv-id-form"

    history = app.handle(
        "conversations.history", {**reader, "conversationId": head["conversationId"]}
    )
    assert [entry["message"] for entry in history["entries"]] == ["hello beta"]
    assert "threadId" not in history


def test_reads_never_list_a_row_at_the_agents_name_uri(golden_daemon) -> None:
    """No name-form read alias (2026-11-19): a row at the name URI is not the agent's."""

    app = golden_daemon(id_form=True).app
    alpha, beta = _address(app, "alpha"), _address(app, "beta")
    owner, node, _segment = parse_agent_uri(beta)
    frozen = canonical_agent_uri(owner, node, "beta")
    assert frozen != beta
    pre_flip = InboxMessage(
        message_id="pre-flip",
        conversation_id="conv-pre-flip",
        sender=alpha,
        recipient=frozen,
        payload=json.dumps({"message": "queued before the flip"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    assert app._inbox.receive(pre_flip).accepted
    sent = app.handle(
        "message.send",
        {
            "from": alpha,
            "sessionRef": SESSIONS["alpha"],
            "to": [beta],
            "summary": "hello",
            "message": "hello beta",
            "conversationId": "conv-post-flip",
            "idempotencyKey": "key-post-flip",
        },
    )
    assert sent["ok"] is True, sent
    reader = {"from": beta, "sessionRef": SESSIONS["beta"]}

    listed = app.handle("conversations.list", reader)
    assert listed["participant"] == beta
    heads = {head["legacyConversationId"]: head for head in listed["conversations"]}
    assert sorted(heads) == ["conv-post-flip"]
    history = app.handle(
        "message.history", {**reader, "conversationId": "conv-pre-flip"}
    )
    assert history["entries"] == []
