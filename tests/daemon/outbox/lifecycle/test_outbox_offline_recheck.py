"""Offline-recipient recheck cadence for outbox and custody rows.

PR #1463 finding 1: deferring an offline row to its own TTL deadline made
``attempts=0`` TTL deaths the guaranteed outcome for any recipient that
stayed unreachable, with the liveliness wake as the row's only rescue.  The
pump now rechecks presence on a bounded cadence instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.daemon.impl.inbox.service.policy import OFFLINE_RECHECK_INTERVAL_MS

SENDER = "agent:owner:node-a:sender"
RECIPIENT = "agent:owner:node-b:recipient"


class _Events:
    def publish(self, _event: object) -> None:
        return None


class _Logger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def log(self, level: str, event: str, **fields: Any) -> None:
        self.events.append((level, event, fields))


def _message(
    message_id: str, *, expires_at_ms: int, recipient: str = RECIPIENT
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation-offline-recheck",
        sender=SENDER,
        recipient=recipient,
        payload=b'{"message":"recheck me while offline"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=0,
        expires_at_ms=expires_at_ms,
    )


def _service(tmp_path: Path, *, logger: _Logger | None = None) -> InboxService:
    return InboxService(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        node_id="offline-recheck-node",
        logger=logger,
    )


def test_offline_row_rechecks_on_bounded_cadence_instead_of_its_ttl(
    tmp_path: Path,
) -> None:
    start = 1_000
    expires = start + 10 * OFFLINE_RECHECK_INTERVAL_MS
    service = _service(tmp_path)
    try:
        # submit() itself takes the first offline deferral.
        service.submit(_message("recheck-cadence", expires_at_ms=expires), now_ms=start)
        item = service.outbox_item("recheck-cadence")
        assert item.attempts == 0
        assert item.next_attempt_ms == start + OFFLINE_RECHECK_INTERVAL_MS
        assert item.next_attempt_ms < expires

        # Still offline: each cadence tick slides the recheck forward by the
        # same bounded step and never parks the row at its expiry deadline.
        [result] = service.retry_due(now_ms=start + OFFLINE_RECHECK_INTERVAL_MS)
        assert result.accepted is True and result.queued is True
        item = service.outbox_item("recheck-cadence")
        assert item.attempts == 0
        assert item.next_attempt_ms == start + 2 * OFFLINE_RECHECK_INTERVAL_MS
        assert service.dlq_count() == 0
    finally:
        service.close()


def test_offline_row_delivers_when_recipient_appears_mid_window(
    tmp_path: Path,
) -> None:
    """The wake coordinator is no longer the row's only rescue path."""

    start = 1_000
    appears_at = start + 2 * OFFLINE_RECHECK_INTERVAL_MS
    expires = start + 10 * OFFLINE_RECHECK_INTERVAL_MS
    service = _service(tmp_path)
    transport: MemoryDeliveryTransport = service._transport  # noqa: SLF001
    try:
        service.submit(_message("mid-window-online", expires_at_ms=expires), now_ms=start)
        transport.set_online(RECIPIENT, False)

        transport.set_online(RECIPIENT, True)
        transport.confirm_direct("mid-window-online")
        [result] = service.retry_due(now_ms=appears_at)
        assert result.accepted is True and result.queued is False
        assert service.outbox_count() == 0
        assert service.dlq_count() == 0
    finally:
        service.close()


def test_offline_recheck_never_defers_past_the_ttl_deadline(tmp_path: Path) -> None:
    start = 1_000
    expires = start + 10_000  # shorter than one recheck interval
    service = _service(tmp_path)
    try:
        service.submit(_message("short-ttl", expires_at_ms=expires), now_ms=start)

        # The cadence never defers past the deadline: submit's first
        # deferral is capped at the TTL itself.
        assert (
            service.outbox_item("short-ttl").next_attempt_ms == expires
        )

        [result] = service.retry_due(now_ms=expires)
        assert result.accepted is False and result.code == "TTL_EXPIRED"
        assert service.dlq_count() == 1
    finally:
        service.close()


def test_offline_hold_is_logged_once_per_stretch(tmp_path: Path) -> None:
    logger = _Logger()
    start = 1_000
    expires = start + 10 * OFFLINE_RECHECK_INTERVAL_MS
    service = _service(tmp_path, logger=logger)
    try:
        service.submit(_message("hold-once", expires_at_ms=expires), now_ms=start)

        # submit() took the first deferral: exactly one hold event.
        holds = [entry for entry in logger.events if entry[1] == "outbox.offline_hold"]
        assert len(holds) == 1
        assert holds[0][0] == "info"
        assert holds[0][2]["messageId"] == "hold-once"
        assert holds[0][2]["recipient"] == RECIPIENT

        # The cadence tick itself stays log-silent.
        service.retry_due(now_ms=start + OFFLINE_RECHECK_INTERVAL_MS)
        holds = [entry for entry in logger.events if entry[1] == "outbox.offline_hold"]
        assert len(holds) == 1

        terminal = [
            entry
            for entry in logger.events
            if entry[1] == "outbox.terminal_failed"
        ]
        assert terminal == []
    finally:
        service.close()


def test_custody_row_rechecks_offline_recipient_on_the_same_cadence(
    tmp_path: Path,
) -> None:
    start = 1_000
    expires = start + 10 * OFFLINE_RECHECK_INTERVAL_MS
    service = _service(tmp_path)
    transport: MemoryDeliveryTransport = service._transport  # noqa: SLF001
    try:
        message = _message("custody-recheck", expires_at_ms=expires)
        service.accept_custody(
            message, mailbox_node="node-b", now_ms=start
        )
        transport.set_online(RECIPIENT, False)

        [result] = service.retry_due(now_ms=start + 1)
        row = service._db.execute(  # noqa: SLF001
            "SELECT attempts, next_attempt_ms, expires_at_ms FROM custody "
            "WHERE message_id = ?",
            ("custody-recheck",),
        ).fetchone()
        assert int(row["attempts"]) == 0
        assert int(row["next_attempt_ms"]) == start + 1 + OFFLINE_RECHECK_INTERVAL_MS
        assert int(row["next_attempt_ms"]) < int(row["expires_at_ms"])
    finally:
        service.close()


def test_offline_death_still_reports_ttl_expired_after_bounded_rechecks(
    tmp_path: Path,
) -> None:
    """The TTL contract is unchanged: expiry wins over the recheck cadence."""

    start = 1_000
    expires = start + 3 * OFFLINE_RECHECK_INTERVAL_MS
    service = _service(tmp_path)
    transport: MemoryDeliveryTransport = service._transport  # noqa: SLF001
    try:
        service.submit(_message("dies-anyway", expires_at_ms=expires), now_ms=start)
        transport.set_online(RECIPIENT, False)

        now = start
        while now + OFFLINE_RECHECK_INTERVAL_MS < expires:
            now += OFFLINE_RECHECK_INTERVAL_MS
            outcomes = service.retry_due(now_ms=now)
            assert outcomes
            for result in outcomes:
                assert result.accepted is True and result.queued is True

        [result] = service.retry_due(now_ms=expires)
        assert result.accepted is False and result.code == "TTL_EXPIRED"
        assert service.outbox_count() == 0
        assert service.dlq_count() == 1
        payload = json.loads(
            service._db.execute(  # noqa: SLF001
                "SELECT payload FROM dlq WHERE message_id = ?", ("dies-anyway",)
            ).fetchone()["payload"]
        )
        assert payload["message"] == "recheck me while offline"
    finally:
        service.close()
