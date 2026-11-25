"""Inbound from an adapter and replies to it (plan slice S1; P1 characterized it).

Discussion record sections 13.4 and 13.7.  Behaviour after slice S1:

- The Lark worker sends inbound messages with the three-segment sender
  ``adapter:lark:<adapter>`` (``adapters/lark/worker/entry.py``,
  ``channel_actor_id``).  ``message.send`` accepts it over IPC and writes
  an inbox row and a sender copy.  ``threads/ids.endpoint_of`` still only
  reads the four-segment form, so the row has no send-link thread (link
  admission is unchanged), but both copies are now recorded under the pair
  key of the adapter and the agent, and the conversation is listed with
  ``threadId`` null.
- The four-segment adapter address keeps its adapter thread and the exact
  cell it had before slice S1 (literal ids pinned).
- A ``route:`` sender, and the retired ``channel:lark:<adapter>`` spelling
  (F1c), are refused over IPC (``SENDER_NOT_AUTHENTICATED``):
  it is not an adapter identity at the caller boundary.
- ``message.reply`` to an ``adapter:lark:`` message is submitted (outbox
  row and sender copy) with no thread, and its sender copy is recorded in
  the same pair conversation as the inbound.  The golden harness has no
  reply bridge, so the reply stays ``REPLY_SETTLEMENT_PENDING``.
- ``message.reply`` to a ``user:<owner>`` message is submitted (outbox row
  and sender copy) on the user thread and recorded once, from the sender
  copy, in the cell it had before slice S1.
"""

from __future__ import annotations

from typing import Any

import pytest

from hyprial.daemon.impl.conversations import conversation_cell_id, conversation_pair_key
from hyprial.kernel import DaemonRequestError
from tests.daemon.acceptance.acceptance_support import send, session
from tests.daemon.conversations.bypass.bypass_support import (
    QUEUE_TABLES,
    boot,
    fed,
    queue_rows,
    settle,
    stored,
)
from tests.daemon.conversations.conversation_support import OWNER_PRINCIPAL
from tests.support.golden.app import (
    ADAPTER,
    BETA,
    NODE,
    OWNER,
    OWNER_USER,
    golden_daemon as golden_daemon,
)

LARK_WORKER_SENDER = f"adapter:lark:{ADAPTER}"
RETIRED_LARK_SENDER = f"channel:lark:{ADAPTER}"
NODE_ADAPTER_SENDER = f"adapter:{OWNER}:{NODE}:{ADAPTER}"
BOTH_COPIES = {"inbox", "sender_copies"}


def _inbound(app: Any, sender: str) -> dict[str, Any]:
    """The Lark worker's call shape (``adapters/lark/worker/daemon_link.py``)."""

    return app.handle(
        "message.send",
        {
            "actor": sender,
            "to": [BETA],
            "message": "hello from the chat",
            "summary": "hello from the chat",
            "conversationId": "lark-conversation-1",
            "providerMetadata": {"senderId": "ou_person", "chatType": "p2p"},
            "idempotencyKey": f"lark-inbound:{sender}:om_1",
        },
        _trusted_message_origin="ipc",
    )


def _list(app: Any, actor_params: dict[str, Any]) -> dict[str, dict[str, Any]]:
    listed = app.handle("conversations.list", actor_params)
    return {str(head["peer"]): head for head in listed["conversations"]}


def test_lark_worker_inbound_is_recorded_under_the_adapter_pair_key(
    golden_daemon, monkeypatch
) -> None:
    booted = boot(golden_daemon, monkeypatch)
    app = booted.app
    sender = LARK_WORKER_SENDER

    answer = _inbound(app, sender)

    [delivery] = answer["deliveries"]
    assert delivery["accepted"] is True and delivery["queued"] is False
    message_id = str(delivery["messageId"])
    rows = stored(app, message_id)
    assert set(rows) == BOTH_COPIES
    assert {row["sender"] for row in rows.values()} == {sender}
    assert {row["thread_id"] for row in rows.values()} == {None}
    assert app._conversation_stamp(sender, BETA) is None

    assert fed(app, message_id) == BOTH_COPIES
    settled = settle(booted)
    pair = conversation_pair_key(LARK_WORKER_SENDER, BETA)
    assert pair is not None and conversation_pair_key(sender, BETA) == pair
    cell_id = conversation_cell_id(OWNER_PRINCIPAL, pair, "lark-conversation-1")
    assert settled.sources(message_id) == ["inbox", "sender_copies"]
    assert settled.cells(message_id) == {cell_id}
    head = _list(app, session(BETA))[sender]
    assert "threadId" not in head
    assert (head["conversationId"], head["kind"]) == (cell_id, None)
    assert (head["messages"], head["messageClass"]) == (1, "interaction")
    page = app.handle(
        "conversations.history", {**session(BETA), "conversationId": cell_id}
    )
    assert [entry["messageId"] for entry in page["entries"]] == [message_id]
    assert "threadId" not in page


def test_node_scoped_adapter_inbound_keeps_its_adapter_thread_and_cell(
    golden_daemon, monkeypatch
) -> None:
    """The four-segment address has a thread; its cell id is the one dev had before S1."""

    booted = boot(golden_daemon, monkeypatch)
    app = booted.app

    [delivery] = _inbound(app, NODE_ADAPTER_SENDER)["deliveries"]

    message_id = str(delivery["messageId"])
    thread_id = app._conversation_stamp(NODE_ADAPTER_SENDER, BETA)
    assert thread_id is not None
    rows = stored(app, message_id)
    assert {row["thread_id"] for row in rows.values()} == {thread_id}
    settled = settle(booted)
    assert settled.sources(message_id) == ["inbox", "sender_copies"]
    assert thread_id == "t.jdknpmo63tkapzzqbsmeehd7xe"
    assert settled.cells(message_id) == {"c.2nwbnegg2lkzyxsnligwjjxvpi"}
    assert conversation_cell_id(OWNER_PRINCIPAL, thread_id, "lark-conversation-1") == (
        "c.2nwbnegg2lkzyxsnligwjjxvpi"
    )
    head = _list(app, session(BETA))[NODE_ADAPTER_SENDER]
    assert "threadId" not in head
    assert (head["kind"], head["messages"]) == (
        "adapter",
        1,
    )
    assert head["conversationId"] == "c.2nwbnegg2lkzyxsnligwjjxvpi"


@pytest.mark.parametrize("sender", [f"route:{ADAPTER}:dm", RETIRED_LARK_SENDER])
def test_non_adapter_sender_is_refused_at_the_ipc_boundary(
    golden_daemon, monkeypatch, sender
) -> None:
    """``route:`` and the retired ``channel:lark:`` spelling cannot send inbound."""

    booted = boot(golden_daemon, monkeypatch)

    with pytest.raises(DaemonRequestError) as raised:
        _inbound(booted.app, sender)

    assert raised.value.code == "SENDER_NOT_AUTHENTICATED"
    for table in QUEUE_TABLES:
        assert sender not in {row["sender"] for row in queue_rows(booted.app, table).values()}
    assert settle(booted).recorded_messages() == {booted.control}


def test_reply_to_lark_worker_message_joins_the_inbound_conversation(
    golden_daemon, monkeypatch
) -> None:
    booted = boot(golden_daemon, monkeypatch)
    app = booted.app
    [delivery] = _inbound(app, LARK_WORKER_SENDER)["deliveries"]

    reply = app.handle(
        "message.reply",
        {
            **session(BETA),
            "messageId": delivery["messageId"],
            "summary": "answer",
            "message": "answer for the chat",
        },
    )

    assert reply["code"] == "REPLY_SETTLEMENT_PENDING"
    assert reply["queued"] is True and reply["replied"] is False
    reply_id = str(reply["replyMessageId"])
    rows = stored(app, reply_id)
    assert set(rows) == {"outbox", "sender_copies"}
    for row in rows.values():
        assert (row["sender"], row["recipient"]) == (BETA, LARK_WORKER_SENDER)
        assert row["thread_id"] is None
    assert fed(app, reply_id) == {"sender_copies"}
    settled = settle(booted)
    pair = conversation_pair_key(BETA, LARK_WORKER_SENDER)
    assert pair is not None
    cell_id = conversation_cell_id(OWNER_PRINCIPAL, pair, "lark-conversation-1")
    assert settled.sources(reply_id) == ["sender_copies"]
    assert settled.cells(reply_id) == settled.cells(str(delivery["messageId"])) == {cell_id}
    assert _list(app, session(BETA))[LARK_WORKER_SENDER]["messages"] == 2


def test_reply_to_user_message_is_recorded_once_from_the_sender_copy(
    golden_daemon, monkeypatch
) -> None:
    """The reply to a user is recorded in the cell dev gave it before slice S1."""

    booted = boot(golden_daemon, monkeypatch)
    app = booted.app
    request_id = str(send(app, OWNER_USER, BETA, "owner", text="a question")["messageId"])

    reply = app.handle(
        "message.reply",
        {
            **session(BETA),
            "messageId": request_id,
            "summary": "answer",
            "message": "an answer",
        },
    )

    assert reply["replied"] is True and reply["queued"] is True
    reply_id = str(reply["replyMessageId"])
    thread_id = app._conversation_stamp(OWNER_USER, BETA)
    assert thread_id is not None
    rows = stored(app, reply_id)
    assert set(rows) == {"outbox", "sender_copies"}
    for row in rows.values():
        assert (row["sender"], row["recipient"]) == (BETA, OWNER_USER)
        assert row["thread_id"] == thread_id

    settled = settle(booted)
    assert settled.sources(request_id) == ["inbox", "sender_copies"]
    assert settled.sources(reply_id) == ["sender_copies"]
    cell_id = conversation_cell_id(OWNER_PRINCIPAL, thread_id, "conv-owner")
    assert (thread_id, cell_id) == (
        "t.cja4j77timli7gjcf5ch3x72tm", "c.ohxebo23rrgm2cvq5xnb34mdum",
    )
    assert settled.cells(request_id) == settled.cells(reply_id) == {cell_id}
    for reader in (session(BETA), {"from": OWNER_USER}):
        heads = app.handle("conversations.list", reader)["conversations"]
        [head] = [item for item in heads if item["conversationId"] == cell_id]
        assert head["conversationId"] == cell_id
        assert head["messages"] == 2
        page = app.handle(
            "conversations.history", {**reader, "conversationId": head["conversationId"]}
        )
        assert sorted(entry["messageId"] for entry in page["entries"]) == sorted(
            [request_id, reply_id]
        )
