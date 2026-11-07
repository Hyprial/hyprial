"""Regressions for outbox online retry timing and submit ambiguity."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.adapters.lark.contracts.reply_bridge import LarkReplyBridgeTransport
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    ZenohDeliveryTransport,
)
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.daemon.impl.inbox.service.policy import (
    RetryPolicy,
    online_retry_next_attempt_ms,
)
from hyprial.daemon.impl.inbox.authority.errors import InboxSubmissionOutcomeUnknown

SENDER = "agent:owner:node-a:sender"
RECIPIENT = "agent:owner:node-b:recipient"


class _Events:
    def publish(self, _event: object) -> None:
        return None


def _message(message_id: str) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation-outbox-reconcile",
        sender=SENDER,
        recipient=RECIPIENT,
        payload=b'{"message":"deliver once"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
    )


def test_liveliness_flaps_cannot_terminalize_before_ttl(
    tmp_path: Path,
) -> None:
    """Online retries remain held until the single TTL deadline."""

    transport = MemoryDeliveryTransport()
    policy = RetryPolicy()
    database = tmp_path / "flapping.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database,
        transport,
        _Events(),
        retry_policy=policy,
    )
    facade = DeliveryCustodyFacade(coordinator, database)
    try:
        facade.submit(_message("flapping"), now_ms=0, defer_direct=True)

        # Four fast online transitions exceed the retired retry counter.
        for now_ms in (100, 200, 300, 400):
            transport.set_online(RECIPIENT, True)
            facade.wake_outbox_recipient(RECIPIENT, now_ms=now_ms)
            facade.retry_due(now_ms=now_ms)
            transport.set_online(RECIPIENT, False)

            assert facade.outbox_count() == 1
            assert facade.dlq_count() == 0

        assert facade.outbox_item("flapping").attempts == 4

        facade.wake_outbox_recipient(RECIPIENT, now_ms=35_999)
        facade.retry_due(now_ms=35_999)
        assert facade.outbox_count() == 1
        assert facade.dlq_count() == 0

        facade.wake_outbox_recipient(RECIPIENT, now_ms=36_000)
        [queued] = facade.retry_due(now_ms=36_000)
        assert queued.accepted is True and queued.queued is True
        assert facade.outbox_count() == 1
        assert facade.dlq_count() == 0
    finally:
        facade.close()


def test_existing_outbox_rows_gain_persistent_retry_and_confirmation_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-outbox.sqlite3"
    service = InboxService(database, MemoryDeliveryTransport())
    service.submit(_message("legacy-row"), now_ms=1_000, defer_direct=True)
    service.close()

    with sqlite3.connect(database) as connection:
        for column in (
            "confirmation_reason",
            "confirmation_attempts",
            "confirmation_started_at_ms",
            "retry_started_at_ms",
        ):
            connection.execute(f"ALTER TABLE outbox DROP COLUMN {column}")

    reopened = InboxService(database, MemoryDeliveryTransport())
    try:
        item = reopened.outbox_item("legacy-row")
        assert item.retry_started_at_ms is not None
        assert item.confirmation_started_at_ms is None
        assert item.confirmation_attempts == 0
    finally:
        reopened.close()


class _HalfOpenCommittedTransport(MemoryDeliveryTransport):
    """The publication committed remotely, but every ACK is lost."""

    def __init__(self) -> None:
        super().__init__()
        self.committed = True
        self.notices: list[InboxMessage] = []

    def deliver_notice(self, node: str, message: InboxMessage) -> bool:
        del node
        self.notices.append(message)
        return True


def test_half_open_delivery_stays_pending_without_confirmation_phase(
    tmp_path: Path,
) -> None:
    transport = _HalfOpenCommittedTransport()
    service = InboxService(
        tmp_path / "half-open.sqlite3",
        transport,
        retry_policy=RetryPolicy(),
    )
    try:
        transport.set_online(RECIPIENT, True)
        service.submit(_message("half-open"), now_ms=0, defer_direct=True)
        [pending] = service.retry_due(now_ms=0)

        assert pending.accepted is True
        assert pending.queued is True
        assert service.outbox_count() == 1
        assert service.dlq_count() == 0
        assert transport.notices == []

        awaiting = service.outbox_item("half-open")
        assert awaiting.confirmation_started_at_ms is None
        assert awaiting.confirmation_attempts == 0
        assert awaiting.next_attempt_ms == online_retry_next_attempt_ms(
            message_id="half-open",
            attempts=1,
            now_ms=0,
            expires_at_ms=awaiting.expires_at_ms,
        )
    finally:
        service.close()


class _LostReplySession:
    def get(self, _key: str, **_kwargs: object) -> tuple[object, ...]:
        return ()


class _RemotePresence:
    def actor_online(self, _recipient: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


def test_zenoh_transport_has_no_confirmation_query() -> None:
    transport = ZenohDeliveryTransport(
        _LostReplySession(),  # type: ignore[arg-type]
        _RemotePresence(),  # type: ignore[arg-type]
        receipt_timeout=0.001,
    )

    assert not hasattr(transport, "confirm_fetch")


def test_reply_bridge_has_no_confirmation_query() -> None:
    class _UnusedAdapterClient:
        def reply_online(self, _adapter: str) -> bool:
            return False

        def deliver_reply(self, _adapter: str, _delivery: object) -> bool:
            return False

    inner = ZenohDeliveryTransport(
        _LostReplySession(),  # type: ignore[arg-type]
        _RemotePresence(),  # type: ignore[arg-type]
        receipt_timeout=0.001,
    )
    transport = LarkReplyBridgeTransport(inner, _UnusedAdapterClient())

    assert not hasattr(transport, "confirm_fetch")


class _BlockingCommittedTransport(MemoryDeliveryTransport):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def is_online(self, _recipient: str) -> bool:
        return True

    def deliver(self, _message: InboxMessage) -> bool:
        self.entered.set()
        assert self.release.wait(2.0)
        return False


def test_committed_submit_timeout_preserves_typed_unknown_with_message_id(
    tmp_path: Path, monkeypatch,
) -> None:
    transport = _BlockingCommittedTransport()
    database = tmp_path / "submit-timeout.sqlite3"
    coordinator = DeliveryCustodyCoordinator(database, transport, _Events())
    facade = DeliveryCustodyFacade(coordinator, database, timeout=0.05)
    from hyprial.daemon.impl.inbox.actor.internal import projection as actor_module
    wait = actor_module._SystemReply.wait

    def wait_after_native_entry(reply, timeout):
        assert transport.entered.wait(2.0)
        return wait(reply, timeout)

    monkeypatch.setattr(actor_module._SystemReply, "wait", wait_after_native_entry)
    try:
        started = time.monotonic()
        with pytest.raises(InboxSubmissionOutcomeUnknown) as caught:
            facade.submit(_message("submit-timeout"), now_ms=0)
        elapsed = time.monotonic() - started

        assert transport.entered.is_set()
        assert elapsed >= 0.04
        assert transport.release.is_set() is False
        assert caught.value.data["messageId"] == "submit-timeout"
        assert caught.value.data["commandAdmission"] == "accepted"
        assert caught.value.data["outcomeKnown"] is False
        assert caught.value.data["submissionCorrelationId"].startswith("inbox:submit:")
        assert caught.value.code == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
        assert facade.outbox_count() == 1
    finally:
        transport.release.set()
        assert coordinator.drain(2.0).complete is True
