"""actor_identity: mailboxes scenarios."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications
from pathlib import Path
import pytest
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
)

from .support import (
    PI_WORKER,
    _registered,
    _restart_application,
    application,
    seed_legacy_row,
)


def test_unknown_bare_name_keeps_one_consumption_key_across_restart(
    tmp_path: Path,
) -> None:
    """Legacy bare-key rows stay consumable across restart.

    NEW sends to a bare name are rejected at the admission gate
    (TARGET_IS_NODE), but rows already stored under a bare key — including
    pre-rename node addresses owned by the pending-migration card — must
    keep their one consumption key.  Seed the row directly (arrival path),
    exactly like the pre-fix legacy row in the agent-destroy drain test.
    """

    app = application(tmp_path, loopback=True)
    assert app._inbox is not None
    legacy = InboxMessage(
        message_id="legacy-bare-1",
        conversation_id="conversation-1",
        sender="restart-node",
        recipient="restart-node",
        payload=b'{"message":"first"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    seed_legacy_row(app._inbox, legacy)

    restarted = _restart_application(app, tmp_path)
    pending = restarted.handle("message.pending.list", {"from": "restart-node"})
    assert [message["messageId"] for message in pending["messages"]] == [
        "legacy-bare-1"
    ]
    acknowledged = restarted.handle(
        "message.ack", {"from": "restart-node", "messageId": "legacy-bare-1"}
    )
    assert acknowledged["acknowledged"] is True
    status = restarted.handle(
        "message.status",
        {"from": "restart-node", "messageId": "legacy-bare-1"},
    )
    assert status["state"] == "fetched"
    assert status["records"][0]["sender"] == "restart-node"


def test_registered_name_status_still_reads_its_bare_spelling_rows(
    tmp_path: Path,
) -> None:
    """A registered agent's rows from the bare-sender era stay readable.

    The sender-auth caller resolves the claim to the canonical URI; the status
    read must still union the caller's own bare spelling, as on dev.
    """

    app = application(tmp_path, loopback=True)
    assert app._inbox is not None
    _registered(app, "legacy-writer")
    seed_legacy_row(
        app._inbox,
        InboxMessage(
            message_id="legacy-bare-registered-1",
            conversation_id="conversation-1",
            sender="legacy-writer",
            recipient="legacy-writer",
            payload=b'{"message":"first"}',
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        ),
    )

    status = app.handle(
        "message.status",
        {"from": "legacy-writer", "messageId": "legacy-bare-registered-1"},
    )

    assert [record["sender"] for record in status["records"]] == ["legacy-writer"]


def test_registered_name_send_and_ack_share_the_canonical_key(tmp_path: Path) -> None:
    app = application(tmp_path, providers=(PI_WORKER,), loopback=True)
    sent = app.handle(
        "message.send",
        {"from": _registered(app, "driver"), "to": ["kanban-sim"], "message": "first", "summary": "first"},
    )
    assert sent["deliveries"][0]["target"] == "agent:op:node-1:kanban-sim"
    pending = app.handle("message.pending.list", {"from": "kanban-sim"})
    assert [message["messageId"] for message in pending["messages"]] == [
        sent["messageId"]
    ]

    acknowledged = app.handle(
        "message.ack",
        {"from": "kanban-sim", "messageId": sent["messageId"]},
    )
    assert acknowledged["acknowledged"] is True


def test_registered_name_reply_uses_the_canonical_consumption_key(
    tmp_path: Path,
) -> None:
    app = application(tmp_path, providers=(PI_WORKER,), loopback=True)
    sent = app.handle(
        "message.send",
        {
            "from": _registered(app, "sender"),
            "to": ["kanban-sim"],
            "message": "question", "summary": "question",
        },
    )

    replied = app.handle(
        "message.reply",
        {
            "from": "kanban-sim",
            "messageId": sent["messageId"],
            "message": "answer", "summary": "answer",
        },
    )

    assert replied["replied"] is True
    assert replied["acknowledged"] is True


def test_reply_to_bare_registered_sender_reaches_the_canonical_mailbox(
    tmp_path: Path,
) -> None:
    """The dispatch incident: a bare --from sender must get its reply.

    A message sent with a bare alias as sender keeps that spelling on the
    pending row.  Before the fix, message.reply queued the answer under the
    bare name too -- an outbox island the registered actor never drains, so
    the reply expired silently.  The reply must resolve the alias with the
    same pipeline message.send uses and land in the canonical mailbox.
    """

    app = application(tmp_path, providers=(PI_WORKER,), loopback=True)
    sent = app.handle(
        "message.send",
        {
            "from": "kanban-sim",
            "to": [_registered(app, "dispatcher")],
            "message": "dispatch", "summary": "dispatch",
        },
    )

    replied = app.handle(
        "message.reply",
        {
            "from": "dispatcher",
            "messageId": sent["messageId"],
            "message": "done", "summary": "done",
        },
    )

    assert replied["replied"] is True
    assert replied["acknowledged"] is True
    pending = app.handle("message.pending.list", {"from": "kanban-sim"})
    assert [item["messageId"] for item in pending["messages"]] == [
        replied["replyMessageId"]
    ]
    assert pending["messages"][0]["from"] == "agent:op:node-1:dispatcher"


def test_reply_to_canonical_sender_reaches_the_same_mailbox(
    tmp_path: Path,
) -> None:
    """Four-segment senders pass through resolution untouched."""

    app = application(tmp_path, providers=(PI_WORKER,), loopback=True)
    sent = app.handle(
        "message.send",
        {
            "from": "agent:op:node-1:kanban-sim",
            "to": [_registered(app, "dispatcher")],
            "message": "dispatch", "summary": "dispatch",
        },
    )

    replied = app.handle(
        "message.reply",
        {
            "from": "dispatcher",
            "messageId": sent["messageId"],
            "message": "done", "summary": "done",
        },
    )

    assert replied["replied"] is True
    pending = app.handle("message.pending.list", {"from": "kanban-sim"})
    assert [item["messageId"] for item in pending["messages"]] == [
        replied["replyMessageId"]
    ]


def test_reply_to_unclaimed_local_identity_lands_in_the_local_inbox(
    tmp_path: Path,
) -> None:
    """A canonical URI naming THIS node is locally deliverable, claimed or not.

    The local-first transport treats a self-node URI as a local delivery
    object (registration governs wake, never deliverability), and the read
    side canonicalizes a bare --from to the same key — so the reply neither
    rots in the outbox nor is refused: it waits in the durable inbox for
    its reader.
    """

    app = application(tmp_path, loopback=True)
    ghost = InboxMessage(
        message_id="ghost-dispatch",
        conversation_id="ghost-conversation",
        sender="agent:op:node-1:ghost",
        recipient="agent:op:node-1:responder",
        payload=b'{"message":"dispatch"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    assert app._inbox.receive(ghost).acknowledged is True
    # The replier is a registered identity (resolve-or-reject, Allen b).
    app.handle("agent.create", {"name": "responder"})

    replied = app.handle(
        "message.reply",
        {"from": "responder", "messageId": "ghost-dispatch", "message": "done", "summary": "done"},
    )

    assert replied["replied"] is True
    assert replied["acknowledged"] is True
    # The reply waits under the unclaimed-but-local identity, and a bare
    # --from read canonicalizes to exactly that key.
    pending = app.handle("message.pending.list", {"from": "ghost"})
    assert [item["messageId"] for item in pending["messages"]] == [
        replied["replyMessageId"]
    ]


def test_registered_name_status_uses_the_canonical_sender_key(tmp_path: Path) -> None:
    app = application(tmp_path, providers=(PI_WORKER,), loopback=True)
    sent = app.handle(
        "message.send",
        {"from": "kanban-sim", "to": ["kanban-sim"], "message": "question", "summary": "question"},
    )
    acknowledged = app.handle(
        "message.ack",
        {"from": "kanban-sim", "messageId": sent["messageId"]},
    )
    assert acknowledged["acknowledged"] is True

    status = app.handle(
        "message.status",
        {"from": "kanban-sim", "messageId": sent["messageId"]},
    )

    assert status["state"] == "fetched"
    # The unfenced bare sender resolves to the registered agent's canonical
    # URI at the daemon send boundary (Allen A): one sender form on record.
    assert status["records"][0]["sender"] == "agent:op:node-1:kanban-sim"


def test_unknown_bare_name_reply_is_rejected_and_stays_pending(
    tmp_path: Path,
) -> None:
    """A reply addressed to a bare (node-shaped) sender fails loudly.

    Legacy pending rows with bare senders still exist (seeded here via the
    arrival path), but queueing the answer under the bare key was an outbox
    island nobody drains — the silent hole.  message.reply now refuses with
    facts and leaves the original pending.
    """

    app = application(tmp_path, loopback=True)
    assert app._inbox is not None
    legacy = InboxMessage(
        message_id="legacy-question",
        conversation_id="conversation-1",
        sender="sender-node",
        recipient="agent:op:node-1:replier",
        payload=b'{"message":"question"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    seed_legacy_row(app._inbox, legacy)
    # The replier is a registered identity (resolve-or-reject, Allen b);
    # the rejection under test is about the reply's RECIPIENT being a node.
    app.handle("agent.create", {"name": "replier"})

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "message.reply",
            {
                "from": "replier",
                "messageId": "legacy-question",
                "message": "answer", "summary": "answer",
            },
        )

    assert failure.value.code == "TARGET_IS_NODE"
    pending = app.handle("message.pending.list", {"from": "replier"})
    assert [item["messageId"] for item in pending["messages"]] == [
        "legacy-question"
    ]
