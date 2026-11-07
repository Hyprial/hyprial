from __future__ import annotations

import json
from pathlib import Path

from hyprial.daemon.impl.adapters.lark import HarnessDelivery
from hyprial.daemon.impl.adapters.lark.contracts.reply_bridge import LarkReplyBridgeTransport
from hyprial.daemon.impl.inbox import (
    DEFAULT_HOLD_TTL_MS,
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
    TerminalState,
)


class FakeAdapterReplyClient:
    def __init__(self, *, online: bool, accepts: bool = True) -> None:
        self.online = online
        self.accepts = accepts
        self.deliveries: list[tuple[str, HarnessDelivery]] = []

    def reply_online(self, adapter: str) -> bool:
        return self.online and adapter == "squire"

    def deliver_reply(self, adapter: str, delivery: HarnessDelivery) -> bool:
        self.deliveries.append((adapter, delivery))
        return self.accepts


def _reply(
    *,
    message_id: str = "msg-bridge-reply",
    payload: bytes | None = None,
    recipient: str = "channel:lark:squire",
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="lark:oc-group:oc-group",
        sender="agent:allen:mac:pi-worker",
        recipient=recipient,
        payload=payload
        or json.dumps(
            {"message": "pong", "replyTo": "msg-lark-inbound"},
            separators=(",", ":"),
        ).encode(),
        intent="reply",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        idempotency_key="reply:msg-lark-inbound",
        created_at_ms=1,
    )


def _service(
    tmp_path: Path, client: FakeAdapterReplyClient
) -> InboxService:
    transport = LarkReplyBridgeTransport(MemoryDeliveryTransport(), client)
    return InboxService(
        tmp_path / "inbox.sqlite3", transport, node_id="sender-daemon"
    )


def test_bridge_success_uses_adapter_native_reply_and_records_terminal_receipt(
    tmp_path: Path,
) -> None:
    client = FakeAdapterReplyClient(online=True)
    service = _service(tmp_path, client)

    result = service.submit(_reply(), now_ms=1_000)

    assert result.accepted is True and result.queued is False
    assert service.outbox_count() == 0
    [(adapter, delivery)] = client.deliveries
    assert adapter == "squire"
    assert delivery.delivery_id == "msg-bridge-reply"
    assert delivery.message_id == "msg-bridge-reply"
    assert delivery.reply_to == "msg-lark-inbound"
    assert delivery.from_actor.actor_id == "agent:allen:mac:pi-worker"
    assert delivery.text == "pong"
    [terminal] = service.delivery_status_records(
        "agent:allen:mac:pi-worker", message_id="msg-bridge-reply"
    )
    assert terminal.state is TerminalState.FETCHED
    assert terminal.holder == "sender-daemon"
    assert terminal.reason == "ACK_RECEIVED"
    service.close()


def test_bridge_success_dual_reads_the_current_adapter_lark_spelling(
    tmp_path: Path,
) -> None:
    """PAC NAMB efeeaf0a01ba dual-read: adapter:lark:squire reaches the same
    real LarkReplyBridgeTransport delivery path as channel:lark:squire does
    in test_bridge_success_uses_adapter_native_reply_and_records_terminal_receipt
    above -- through the real InboxService, not a fake."""

    client = FakeAdapterReplyClient(online=True)
    service = _service(tmp_path, client)

    result = service.submit(_reply(recipient="adapter:lark:squire"), now_ms=1_000)

    assert result.accepted is True and result.queued is False
    assert service.outbox_count() == 0
    [(adapter, delivery)] = client.deliveries
    assert adapter == "squire"
    assert delivery.reply_to == "msg-lark-inbound"
    assert delivery.text == "pong"
    service.close()


def test_bridge_offline_is_durable_and_does_not_claim_delivery(
    tmp_path: Path,
) -> None:
    client = FakeAdapterReplyClient(online=False)
    service = _service(tmp_path, client)

    result = service.submit(_reply(), now_ms=1_000)

    assert result.accepted is True and result.queued is True
    assert service.outbox_count() == 1
    assert service.outbox_item("msg-bridge-reply").attempts == 0
    assert client.deliveries == []
    assert service.delivery_status_records("agent:allen:mac:pi-worker") == ()
    service.close()


def test_bridge_send_failure_retries_instead_of_acknowledging(
    tmp_path: Path,
) -> None:
    client = FakeAdapterReplyClient(online=True, accepts=False)
    service = _service(tmp_path, client)

    result = service.submit(_reply(), now_ms=1_000)

    assert result.accepted is True and result.queued is True
    assert service.outbox_item("msg-bridge-reply").attempts == 1
    assert len(client.deliveries) == 1
    assert service.delivery_status_records("agent:allen:mac:pi-worker") == ()
    service.close()


def test_bridge_delegates_receipt_free_system_notice() -> None:
    inner = MemoryDeliveryTransport()
    bridge = LarkReplyBridgeTransport(
        inner, FakeAdapterReplyClient(online=False)
    )
    notice = _reply(message_id="notice-1")

    assert bridge.deliver_notice("sender-node", notice) is True
    assert inner.notices == [("sender-node", notice)]


def test_legacy_stuck_reply_expires_observably_when_adapter_stays_offline(
    tmp_path: Path,
) -> None:
    client = FakeAdapterReplyClient(online=False)
    service = _service(tmp_path, client)
    # Production rows created before the bridge only contain `message`; their
    # `reply:<inbound-id>` idempotency key is the compatibility correlation.
    legacy = _reply(payload=b'{"message":"legacy pong"}')
    service.submit(legacy, now_ms=1_000)

    service.retry_due(now_ms=1_000 + DEFAULT_HOLD_TTL_MS + 1)

    assert service.outbox_count() == 0
    [terminal] = service.delivery_status_records(
        "agent:allen:mac:pi-worker", message_id="msg-bridge-reply"
    )
    assert terminal.state is TerminalState.EXPIRED
    assert terminal.holder == "sender-daemon"
    assert terminal.reason == "TTL_EXPIRED"
    service.close()


def test_legacy_stuck_reply_uses_idempotency_key_as_native_correlation(
    tmp_path: Path,
) -> None:
    client = FakeAdapterReplyClient(online=True)
    service = _service(tmp_path, client)
    legacy = _reply(payload=b'{"message":"legacy pong"}')

    result = service.submit(legacy, now_ms=1_000)

    assert result.accepted is True and result.queued is False
    [(_adapter, delivery)] = client.deliveries
    assert delivery.reply_to == "msg-lark-inbound"
    assert delivery.text == "legacy pong"
    [terminal] = service.delivery_status_records(
        "agent:allen:mac:pi-worker", message_id="msg-bridge-reply"
    )
    assert terminal.state is TerminalState.FETCHED
    assert terminal.reason == "ACK_RECEIVED"
    service.close()
