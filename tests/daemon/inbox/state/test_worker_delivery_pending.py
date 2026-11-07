from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.authority.read import delivery_pending
from hyprial.daemon.impl.inbox.service.state import ConsumptionState

RECIPIENT = "agent:alice:mac:worker"


def _message(message_id: str) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation",
        sender="agent:alice:mac:requester",
        recipient=RECIPIENT,
        payload=b"opaque",
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def test_worker_delivery_pending_uses_the_authority_predicate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inbox.sqlite3"
    inbox = InboxService(database, MemoryDeliveryTransport())
    pending = _message("pending")
    consumed = _message("consumed")
    expired = _message("expired")
    failed = _message("terminal-failed")
    try:
        for item in (pending, consumed, expired, failed):
            assert inbox.receive(item, now_ms=1).acknowledged
        assert inbox.ack(RECIPIENT, consumed.message_id).acknowledged
        settlement = inbox.settle_harness_failure(
            RECIPIENT,
            failed.message_id,
            "PROVIDER_INVALID_REQUEST",
            permanent=True,
            max_attempts=1,
            backoff_ms=(1,),
            now_ms=2,
        )
        assert settlement.terminal

        assert (
            delivery_pending(database, RECIPIENT, pending.message_id, now_ms=2)
            is ConsumptionState.PENDING
        )
        assert delivery_pending(
            database, RECIPIENT, consumed.message_id, now_ms=2
        ) is ConsumptionState.CONSUMED
        assert delivery_pending(
            database, RECIPIENT, expired.message_id, now_ms=10**15
        ) is ConsumptionState.EXPIRED
        assert (
            delivery_pending(database, RECIPIENT, failed.message_id, now_ms=2)
            is ConsumptionState.FAILED  # a terminal failure, not a consumption
        )
        assert (
            delivery_pending(database, RECIPIENT, "missing", now_ms=2)
            is ConsumptionState.UNKNOWN
        )
    finally:
        inbox.close()


def test_worker_delivery_pending_fails_open_when_the_projection_is_unreadable(
    tmp_path: Path,
) -> None:
    """A missing or damaged inbox must not silently drop every worker turn."""

    assert (
        delivery_pending(tmp_path / "absent.sqlite3", RECIPIENT, "m1")
        is ConsumptionState.UNKNOWN
    )
    damaged = tmp_path / "damaged.sqlite3"
    damaged.write_bytes(b"not a sqlite database")
    assert delivery_pending(damaged, RECIPIENT, "m1") is ConsumptionState.UNKNOWN


def test_worker_delivery_pending_fails_open_on_non_sqlite_projection_errors(
    tmp_path: Path, monkeypatch,
) -> None:
    database = tmp_path / "inbox.sqlite3"
    database.touch()
    warnings: list[tuple[str, str]] = []

    def fail_read(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("projection decode failed")

    monkeypatch.setattr(
        "hyprial.daemon.impl.inbox.authority.read.InboxReadProjection.delivery_state",
        fail_read,
    )
    assert delivery_pending(
        database,
        RECIPIENT,
        "m1",
        warn=lambda kind, detail: warnings.append((kind, detail)),
    ) is ConsumptionState.UNKNOWN
    assert warnings == [("ValueError", "projection decode failed")]
