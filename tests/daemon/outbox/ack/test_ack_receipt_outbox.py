"""Consumer ack must never strand the sender's outbox.

Incident shape: during an MCP disconnect the recipient consumed a delivered
message through the CLI wire (`hyprial message ack` → daemon ``message.ack``) while the
sender's outbox still held the entry because every delivery-receipt query had
failed ("receipt not confirmed", attempts climbing).  The receipt is answered
from ``has_received``, which must remain true after the consumer ack so the
sender's next retry confirms and clears the outbox instead of walking the
entry into the DLQ.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

from pathlib import Path

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

ACTOR = "agent:allen:mac:hq-adjutant"


class _FlakyReceiptTransport:
    """Deliver into the recipient service; receipt confirmation is switchable."""

    def __init__(self) -> None:
        self.recipient: InboxService | None = None
        self.receipt_broken = True

    def is_online(self, recipient: str) -> bool:
        return True

    def deliver(self, message: InboxMessage) -> bool:
        assert self.recipient is not None
        self.recipient.receive(message)
        if self.receipt_broken:
            return False
        return self.recipient.has_received(message.message_id)

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        return False


class _Presence:
    def online_actors(self) -> tuple[str, ...]:
        return (ACTOR,)

    def actor_online(self, actor: str) -> bool:
        return actor == ACTOR

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


def _recipient_application(tmp_path: Path, service: InboxService) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        node_id="recipient-node",
    **_deep_module_daemon_dependencies())
    app._inbox = service
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    return app


def test_cli_ack_then_receipt_heal_clears_sender_outbox(tmp_path: Path) -> None:
    transport = _FlakyReceiptTransport()
    sender = InboxService(tmp_path / "sender.sqlite3", transport)
    recipient_service = InboxService(
        tmp_path / "recipient.sqlite3", MemoryDeliveryTransport()
    )
    transport.recipient = recipient_service
    recipient = _recipient_application(tmp_path, recipient_service)

    message = InboxMessage(
        message_id="msg-incident",
        conversation_id="conv-1",
        sender="agent:op:sender-node:cli",  # post-A wire shape: senders are canonical
        recipient=ACTOR,
        payload=b'{"message":"please ack"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
    )
    submitted = sender.submit(message, now_ms=0)

    # The incident state: recipient holds the message, sender still retries.
    assert submitted.accepted and submitted.queued
    assert sender.outbox_count() == 1
    assert sender.outbox_item("msg-incident").attempts == 1
    assert recipient_service.pending_count(ACTOR) == 1

    # The exact CLI wire: `hyprial message ack --from <actor> <message-id>`.
    acked = recipient.handle(
        "message.ack", {"from": ACTOR, "messageId": "msg-incident"}
    )
    assert acked["ok"] is True and acked["acknowledged"] is True
    assert recipient_service.pending_count(ACTOR) == 0

    # Receipt path heals: the next retry must confirm against the consumed
    # (but received) message and clear the outbox — not re-deliver, not DLQ.
    transport.receipt_broken = False
    sender.retry_due(now_ms=5_000)

    assert sender.outbox_count() == 0
    assert sender.dlq_count() == 0
    assert recipient_service.pending_count(ACTOR) == 0


def test_receipt_still_broken_keeps_retrying_without_duplicating_inbox(
    tmp_path: Path,
) -> None:
    transport = _FlakyReceiptTransport()
    sender = InboxService(tmp_path / "sender.sqlite3", transport)
    recipient_service = InboxService(
        tmp_path / "recipient.sqlite3", MemoryDeliveryTransport()
    )
    transport.recipient = recipient_service

    message = InboxMessage(
        message_id="msg-still-broken",
        conversation_id="conv-2",
        sender="agent:op:sender-node:cli",  # post-A wire shape: senders are canonical
        recipient=ACTOR,
        payload=b'{"message":"hi"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
    )
    sender.submit(message, now_ms=0)
    recipient_service.ack(ACTOR, "msg-still-broken")

    sender.retry_due(now_ms=2_000)

    # Attempts keep climbing while the receipt path is down, and the duplicate
    # redelivery must not resurrect the consumed message on the recipient.
    assert sender.outbox_item("msg-still-broken").attempts == 2
    assert recipient_service.pending_count(ACTOR) == 0
