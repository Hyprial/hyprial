from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    HoldReason,
    InboxMessage,
    InboxService,
    LocalFirstDeliveryTransport,
    MemoryDeliveryTransport,
    TerminalState,
)


def message(recipient: str, message_id: str = "msg-local") -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation-local",
        sender="agent:owner:host:watcher",
        recipient=recipient,
        payload=b"work",
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
    )


def test_registered_local_actor_delivers_into_same_durable_inbox(
    tmp_path: Path,
) -> None:
    actor = "agent:owner:host:worker"
    network = MemoryDeliveryTransport()
    delivery = LocalFirstDeliveryTransport(network)
    inbox = InboxService(tmp_path / "inbox.sqlite3", delivery)
    delivery.bind_receiver(inbox.receive)
    registration = delivery.register_actor(actor)
    try:
        result = inbox.submit(message(actor), now_ms=1)

        assert result.accepted is True
        assert result.queued is False
        assert inbox.outbox_count() == 0
        assert inbox.next(actor) == message(actor)
        assert network.delivered == []
    finally:
        registration.close()
        inbox.close()


def test_local_direct_delivery_records_an_authoritative_fetched_verdict(
    tmp_path: Path,
) -> None:
    """The crossing point of #101 and the terminal-state rework.

    A message delivered to a managed local actor bypasses Zenoh entirely, and
    that bypass must not reopen the "undecidable delivery" hole: the sender
    asking "what happened to my message" must read ``fetched``, and it must be
    the recipient's own record (written inside the transaction that committed
    the message), not the holder's second-hand receipt mirror.
    """

    actor = "agent:owner:host:worker"
    sender = "agent:owner:host:watcher"
    network = MemoryDeliveryTransport()
    delivery = LocalFirstDeliveryTransport(network)
    inbox = InboxService(tmp_path / "inbox.sqlite3", delivery, node_id="here")
    delivery.bind_receiver(inbox.receive)
    registration = delivery.register_actor(actor)
    try:
        result = inbox.submit(message(actor), now_ms=1)
        assert result.accepted is True

        records = inbox.delivery_status_records(sender)
        assert [record.message_id for record in records] == ["msg-local"]
        record = records[0]
        assert record.state is TerminalState.FETCHED
        # Recipient-observed evidence: the local path writes the authoritative
        # ACK record first and the holder mirror must not displace it.
        assert record.reason == HoldReason.ACK_RECEIVED.value
        assert record.holder == "here"
        assert network.delivered == []
    finally:
        registration.close()
        inbox.close()


def test_closing_local_actor_registration_restores_network_delivery(
    tmp_path: Path,
) -> None:
    actor = "agent:owner:host:worker"
    network = MemoryDeliveryTransport()
    network.set_online(actor, True)
    network.confirm_direct("msg-network")
    delivery = LocalFirstDeliveryTransport(network)
    inbox = InboxService(tmp_path / "inbox.sqlite3", delivery)
    delivery.bind_receiver(inbox.receive)
    registration = delivery.register_actor(actor)
    registration.close()
    try:
        outbound = message(actor, "msg-network")
        result = inbox.submit(outbound, now_ms=1)

        assert result.queued is False
        assert network.delivered == [outbound.message_id]
        assert inbox.next(actor) is None
    finally:
        inbox.close()


def test_local_registration_is_reference_counted(tmp_path: Path) -> None:
    actor = "agent:owner:host:worker"
    network = MemoryDeliveryTransport()
    delivery = LocalFirstDeliveryTransport(network)
    inbox = InboxService(tmp_path / "inbox.sqlite3", delivery)
    delivery.bind_receiver(inbox.receive)
    first = delivery.register_actor(actor)
    second = delivery.register_actor(actor)
    first.close()
    try:
        assert delivery.is_online(actor) is True
        second.close()
        assert delivery.is_online(actor) is False
    finally:
        inbox.close()
