"""Mechanism traffic in the conversation record (plan slice S1; P1 characterized it).

Discussion record sections 13.2 (rows 5 to 7) and 13.5 (hook exchange).
P1 pinned that none of these rows was recorded.  Slice S1 (rulings R4, R6,
D5) changes that:

- A dispatch-prefixed message from the dispatch service actor and the
  worker's ``message.reply`` to it (idempotency key
  ``reply:workflow-<uuid>``) are SYSTEM.  They are now handed to the
  recorder with their class, recorded with ``messageClass: system`` under
  the pair key of the dispatch actor and the worker (the dispatch actor is
  reserved and has no send-link thread), listed by ``conversations.list``
  with ``messageClass: system`` and ``threadId`` null, and readable through
  ``conversations.history``.
- A ``system:hyprial:<node>`` notice has no peer actor (D5): the
  ``intent == "system"`` notice is still not fed (intent filter), and an
  ``intent == "request"`` look-alike is fed and skipped as ``unthreaded``.
- A hook exchange (sender ``_turn-hooks``, a reserved name) is recorded
  under its pair key: the ``ONLINE_ONLY`` request (one inbox row) and the
  handler's durable reply (inbox row and sender copy).  Its class is
  INTERACTION under the class rulebook.  Since slice H1 every exchange
  with one handler uses the default conversation of the pair, so ten
  exchanges share one cell and one ``conversations.list`` head.
- Admission is unchanged: the reserved actors still have no send-link
  thread and ``threads.list`` names no pair key.
"""

from __future__ import annotations

import json
from typing import Any

from hyprial.daemon.impl.conversations import conversation_cell_id, conversation_pair_key
from hyprial.daemon.impl.conversations.ids import default_conversation_id, endpoint_key
from hyprial.daemon.impl.harnesses.turn_delivery.service import InboxTurnHookInvoker
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.envelope.rules import message_class_of
from hyprial.kernel import (
    DISPATCH_MESSAGE_PREFIX,
    TURN_HOOKS_ACTOR_NAME,
    MessageClass,
    canonical_agent_uri,
)
from tests.daemon.acceptance.acceptance_support import session
from tests.daemon.conversations.bypass.bypass_support import (
    boot,
    fed,
    recorded,
    settle,
    stored,
)
from tests.daemon.conversations.conversation_support import OWNER_PRINCIPAL
from tests.support.golden.app import (
    BETA,
    NODE,
    OWNER,
    golden_daemon as golden_daemon,
)

DISPATCH_ID = f"{DISPATCH_MESSAGE_PREFIX}00000000-0000-4000-8000-000000000001"
SYSTEM_SENDER = f"system:hyprial:{NODE}"
TURN_HOOKS = canonical_agent_uri(OWNER, NODE, TURN_HOOKS_ACTOR_NAME)
BOTH_COPIES = ["inbox", "sender_copies"]


def _received(booted: Any, message_id: str, *, sender: str, intent: str) -> InboxMessage:
    message = InboxMessage(
        message_id=message_id,
        conversation_id="conv-mechanism",
        sender=sender,
        recipient=BETA,
        payload=b'{"message":"mechanism traffic"}',
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=booted.daemon.clock.ms(),
    )
    assert booted.app._inbox.receive(message).accepted is True
    return message


def _reply(app: Any, message_id: str) -> str:
    reply = app.handle(
        "message.reply",
        {
            **session(BETA),
            "messageId": message_id,
            "summary": "done",
            "message": "done",
        },
    )
    assert reply["replied"] is True
    return str(reply["replyMessageId"])


def _head(app: Any, cell_id: str) -> dict[str, Any]:
    heads = app.handle("conversations.list", session(BETA))["conversations"]
    [head] = [item for item in heads if item["conversationId"] == cell_id]
    return head


def test_pac_dispatch_and_worker_reply_are_recorded_as_system(
    golden_daemon, monkeypatch
) -> None:
    booted = boot(golden_daemon, monkeypatch)
    app = booted.app
    coordinator = app._dispatch_service_actor

    dispatch = _received(booted, DISPATCH_ID, sender=coordinator, intent="request")
    reply_id = _reply(app, DISPATCH_ID)

    assert message_class_of(dispatch) is MessageClass.SYSTEM
    assert set(stored(app, DISPATCH_ID)) == {"inbox"}
    reply_rows = stored(app, reply_id)
    assert set(reply_rows) == set(BOTH_COPIES)
    for row in reply_rows.values():
        assert row["idempotency_key"] == f"reply:{DISPATCH_ID}"
        assert (row["sender"], row["recipient"]) == (BETA, coordinator)
    assert fed(app, DISPATCH_ID) == {"inbox"}
    assert fed(app, reply_id) == set(BOTH_COPIES)
    assert app._conversation_stamp(coordinator, BETA) is None

    settled = settle(booted)
    pair = conversation_pair_key(coordinator, BETA)
    assert pair is not None
    cell_id = conversation_cell_id(OWNER_PRINCIPAL, pair, "conv-mechanism")
    assert settled.sources(DISPATCH_ID) == ["inbox"]
    assert settled.sources(reply_id) == BOTH_COPIES
    assert settled.cells(DISPATCH_ID) == settled.cells(reply_id) == {cell_id}
    lines = [line for line in recorded(app) if line["cellId"] == cell_id]
    assert sorted((line["messageId"], line["source"]) for line in lines) == sorted(
        [(DISPATCH_ID, "inbox"), (reply_id, "inbox"), (reply_id, "sender_copies")]
    )
    assert {(line["threadId"], line["messageClass"]) for line in lines} == {(pair, "system")}

    head = _head(app, cell_id)
    assert "threadId" not in head
    assert (head["kind"], head["peer"], head["messageClass"]) == (
        None, coordinator, "system",
    )
    assert head["messages"] == 2
    page = app.handle(
        "conversations.history", {**session(BETA), "conversationId": cell_id, "mode": "tail"}
    )
    assert "threadId" not in page and page["legacyConversationId"] == "conv-mechanism"
    assert sorted(
        (entry["messageId"], entry["messageClass"]) for entry in page["entries"]
    ) == sorted([(DISPATCH_ID, "system"), (reply_id, "system")])


def test_a_daemon_system_notice_has_no_peer_and_stays_unrecorded(
    golden_daemon, monkeypatch
) -> None:
    """D5: a ``system:`` sender is not an addressed actor, so it has no pair key."""

    booted = boot(golden_daemon, monkeypatch)
    app = booted.app

    notice = _received(booted, "notice-1", sender=SYSTEM_SENDER, intent="system")
    look_alike = _received(booted, "notice-2", sender=SYSTEM_SENDER, intent="request")

    assert message_class_of(notice) is MessageClass.SYSTEM
    assert message_class_of(look_alike) is MessageClass.INTERACTION
    assert set(stored(app, "notice-1")) == {"inbox"}
    assert fed(app, "notice-1") == set()
    assert fed(app, "notice-2") == {"inbox"}

    settled = settle(booted)
    assert settled.skip_reasons("notice-1") == {}
    assert settled.skip_reasons("notice-2") == {"inbox": "unthreaded"}
    assert settled.sources("notice-1") == settled.sources("notice-2") == []
    heads = app.handle("conversations.list", session(BETA))["conversations"]
    assert SYSTEM_SENDER not in {head["peer"] for head in heads}


def _exchange(app: Any, seen: frozenset[str] = frozenset()) -> tuple[InboxMessage, str]:
    invoker = InboxTurnHookInvoker(app._inbox, callback_actor=TURN_HOOKS)
    invoker.notify(BETA, {"event": "turn-started"}, timeout_seconds=1.0)
    [request] = [
        message
        for message in app._inbox.pending_messages(BETA)
        if message.sender == TURN_HOOKS and message.message_id not in seen
    ]
    return request, _reply(app, request.message_id)


def test_hook_exchange_rows_are_recorded_under_their_pair_key(
    golden_daemon, monkeypatch
) -> None:
    booted = boot(golden_daemon, monkeypatch)
    app = booted.app

    request, reply_id = _exchange(app)

    assert request.conversation_id == default_conversation_id(
        endpoint_key(TURN_HOOKS), endpoint_key(BETA)
    )
    assert request.lifecycle is DeliveryLifecycle.ONLINE_ONLY
    assert message_class_of(request) is MessageClass.INTERACTION
    request_rows = stored(app, request.message_id)
    assert set(request_rows) == {"inbox"}
    assert request_rows["inbox"]["thread_id"] is None
    assert app._conversation_stamp(TURN_HOOKS, BETA) is None
    reply_rows = stored(app, reply_id)
    assert set(reply_rows) == set(BOTH_COPIES)
    for row in reply_rows.values():
        assert (row["sender"], row["recipient"]) == (BETA, TURN_HOOKS)
        assert row["conversation_id"] == request.conversation_id
        assert row["thread_id"] is None

    assert fed(app, request.message_id) == {"inbox"}
    assert fed(app, reply_id) == set(BOTH_COPIES)
    settled = settle(booted)
    pair = conversation_pair_key(TURN_HOOKS, BETA)
    assert pair is not None
    cell_id = conversation_cell_id(OWNER_PRINCIPAL, pair, request.conversation_id)
    assert settled.sources(request.message_id) == ["inbox"]
    assert settled.sources(reply_id) == BOTH_COPIES
    assert settled.cells(request.message_id) == settled.cells(reply_id) == {cell_id}
    head = _head(app, cell_id)
    assert "threadId" not in head
    assert (head["peer"], head["messageClass"]) == (
        TURN_HOOKS, "interaction",
    )


def test_hook_exchanges_with_one_handler_share_one_conversation(
    golden_daemon, monkeypatch
) -> None:
    """Slice H1: ten exchanges with one handler open one conversation, not ten."""

    booted = boot(golden_daemon, monkeypatch)
    app = booted.app

    exchanges: list[tuple[InboxMessage, str]] = []
    for _ in range(10):
        seen = frozenset(request.message_id for request, _ in exchanges)
        exchanges.append(_exchange(app, seen))

    assert len({request.message_id for request, _ in exchanges}) == 10
    settled = settle(booted)
    cells = set()
    for request, reply_id in exchanges:
        assert settled.cells(request.message_id) == settled.cells(reply_id)
        cells |= settled.cells(request.message_id)
    [cell_id] = cells
    heads = app.handle("conversations.list", session(BETA))["conversations"]
    hook_heads = [head for head in heads if head["peer"] == TURN_HOOKS]
    assert [head["conversationId"] for head in hook_heads] == [cell_id]
    assert hook_heads[0]["messages"] == 20

    replies = {item.message_id: item for item in app._inbox.pending_messages(TURN_HOOKS)}
    assert {
        reply_id: json.loads(replies[reply_id].payload)["replyTo"]
        for _, reply_id in exchanges
    } == {reply_id: request.message_id for request, reply_id in exchanges}
    invoker = InboxTurnHookInvoker(app._inbox, callback_actor=TURN_HOOKS)
    assert invoker._reply_and_sweep_stale("turn-hook-none", BETA) is None  # noqa: SLF001
    assert app._inbox.pending_messages(TURN_HOOKS) == ()
