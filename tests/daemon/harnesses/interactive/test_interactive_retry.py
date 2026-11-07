"""Pull/fetch retirement, retry tiers, and true non-consumption alarms."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    HoldReason,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
    RetryPolicy,
)

INTERACTIVE_ACTOR = "agent:alice:mac:coordinator"


def message(message_id: str) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="lark:chat:test",
        sender="channel:lark:test-app",
        recipient=INTERACTIVE_ACTOR,
        payload=b"opaque-frame",
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
    )


class CommitWithoutReceiptTransport:
    """Reproduce inbox + outbox coexistence with a missing direct receipt."""

    def __init__(self) -> None:
        self.service: InboxService | None = None

    def is_online(self, recipient: str) -> bool:
        return recipient == INTERACTIVE_ACTOR

    def deliver(self, item: InboxMessage) -> bool:
        assert self.service is not None
        self.service.receive(item, now_ms=1)
        return False

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()

    def transfer_custody(self, mailbox: str, item: InboxMessage) -> bool:
        return False

    def deliver_notice(self, node: str, item: InboxMessage) -> bool:
        return False


def loopback_service(
    tmp_path: Path,
    *,
    retry_policy: RetryPolicy | None = None,
    alarms: list[tuple[str, str]] | None = None,
    texts: list[str] | None = None,
) -> InboxService:
    transport = CommitWithoutReceiptTransport()
    service = InboxService(
        tmp_path / "inbox.sqlite3",
        transport,  # type: ignore[arg-type]
        retry_policy=retry_policy,
        interactive_recipient=lambda recipient: recipient == INTERACTIVE_ACTOR,
        alarm_human_delivery=(
            None
            if alarms is None
            else lambda alarm, text: (
                not (
                    alarms.append((alarm.message_id, alarm.reason))
                    or (texts is not None and texts.append(text))
                )
            )
        ),
    )
    transport.service = service
    return service


def test_retry_policy_no_longer_exposes_attempt_budget() -> None:
    policy = RetryPolicy()

    assert not hasattr(policy, "backoff_seconds")
    assert not hasattr(policy, "maximum_attempts")


def test_retry_policy_no_longer_exposes_confirmation_budget() -> None:
    policy = RetryPolicy()

    assert not hasattr(policy, "confirmation_backoff_seconds")
    assert not hasattr(policy, "confirmation_window_seconds")


def test_schema_upgrade_adds_fetch_marker_without_claiming_old_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE inbox (
                   arrival_id INTEGER PRIMARY KEY AUTOINCREMENT,
                   message_id TEXT NOT NULL UNIQUE,
                   conversation_id TEXT NOT NULL,
                   sender TEXT NOT NULL,
                   recipient TEXT NOT NULL,
                   payload BLOB NOT NULL,
                   intent TEXT NOT NULL,
                   lifecycle TEXT NOT NULL,
                   idempotency_key TEXT,
                   created_at_ms INTEGER NOT NULL,
                   received_at_ms INTEGER NOT NULL,
                   consumed INTEGER NOT NULL DEFAULT 0,
                   acknowledged_at_ms INTEGER
               )"""
        )
        connection.execute(
            """INSERT INTO inbox(
                   message_id, conversation_id, sender, recipient, payload,
                   intent, lifecycle, created_at_ms, received_at_ms
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "msg-legacy-pending",
                "conversation",
                "channel:lark:test-app",
                INTERACTIVE_ACTOR,
                b"opaque",
                "request",
                DeliveryLifecycle.DURABLE_SERVICE.value,
                0,
                1,
            ),
        )

    service = InboxService(database, MemoryDeliveryTransport())
    try:
        assert not service.has_fetched("msg-legacy-pending")
        assert [
            # Read paths hide rows past their TTL; read at the row's synthetic clock.
            item.message_id
            for item in service.fetch_pending(INTERACTIVE_ACTOR, now_ms=2)
        ] == ["msg-legacy-pending"]
        assert service.has_fetched("msg-legacy-pending")
    finally:
        service.close()


def test_background_poll_does_not_retire_but_real_fetch_retires_batch(
    tmp_path: Path,
) -> None:
    service = loopback_service(tmp_path)
    items = [message(f"msg-fetch-{index}") for index in range(1, 4)]
    try:
        for item in items:
            assert service.submit(item, now_ms=0).queued

        assert service.outbox_count() == 3
        assert [
            item.message_id
            for item in service.pending_messages(INTERACTIVE_ACTOR, now_ms=1)
        ] == [item.message_id for item in items]
        assert service.outbox_count() == 3
        assert all(not service.has_fetched(item.message_id) for item in items)

        fetched = service.fetch_pending(INTERACTIVE_ACTOR, now_ms=500)

        assert [item.message_id for item in fetched] == [
            item.message_id for item in items
        ]
        assert service.outbox_count() == 0
        assert service.dlq_count() == 0
        assert all(service.has_fetched(item.message_id) for item in items)
        assert {
            record.reason for record in service.delivery_status_records(items[0].sender)
        } == {HoldReason.ACK_RECEIVED.value}
    finally:
        service.close()


def test_direct_receipt_still_retires_immediately(tmp_path: Path) -> None:
    transport = MemoryDeliveryTransport()
    transport.set_online(INTERACTIVE_ACTOR, True)
    transport.confirm_direct("msg-direct")
    service = InboxService(tmp_path / "inbox.sqlite3", transport)
    item = message("msg-direct")
    try:
        result = service.submit(item, now_ms=0)

        assert result.accepted and not result.queued
        assert service.outbox_count() == 0
        assert service.delivery_status_records(item.sender)[0].reason == (
            HoldReason.ACK_RECEIVED.value
        )
    finally:
        service.close()


def test_ack_without_prior_read_also_retires_same_daemon_hold(tmp_path: Path) -> None:
    service = loopback_service(tmp_path)
    item = message("msg-direct-ack")
    try:
        assert service.submit(item, now_ms=0).queued
        assert service.outbox_count() == 1

        assert service.ack(INTERACTIVE_ACTOR, item.message_id).acknowledged

        assert service.has_fetched(item.message_id)
        assert service.outbox_count() == 0
        assert service.dlq_count() == 0
    finally:
        service.close()


def test_unfetched_interactive_message_dead_letters_and_alarms_at_ttl(
    tmp_path: Path,
) -> None:
    alarms: list[tuple[str, str]] = []
    service = loopback_service(
        tmp_path,
        retry_policy=RetryPolicy(),
        alarms=alarms,
    )
    item = message("msg-never-fetched")
    try:
        service.submit(item, now_ms=0)
        service.retry_due(now_ms=service.durable_ttl_ms)

        assert service.has_received(item.message_id)
        assert not service.has_fetched(item.message_id)
        assert service.outbox_count() == 0
        assert service.dlq_count() == 1
        assert alarms == [(item.message_id, "TTL_EXPIRED")]
    finally:
        service.close()


def test_ttl_alarm_tells_the_sender_the_deadline_fact(
    tmp_path: Path,
) -> None:
    """The acceptance runs the real TTL path, not ``render`` directly.

    This transport is the production shape in miniature: the recipient
    COMMITS the message and the direct receipt never comes back.  So at the
    moment the alarm is raised, ``has_received`` is already true. The sender's
    held copy still leaves only at TTL because it never observed the ACK.
    """

    alarms: list[tuple[str, str]] = []
    texts: list[str] = []
    service = loopback_service(
        tmp_path,
        retry_policy=RetryPolicy(),
        alarms=alarms,
        texts=texts,
    )
    item = message("msg-unconfirmed")
    try:
        service.submit(item, now_ms=0)
        service.retry_due(now_ms=service.durable_ttl_ms)

        assert alarms == [(item.message_id, "TTL_EXPIRED")]
        # The message WAS received; only the receipt is missing.
        assert service.has_received(item.message_id)
        assert not service.has_fetched(item.message_id)
        [text] = texts
        assert "TTL 到期" in text
        assert "投递失败" not in text
    finally:
        service.close()
