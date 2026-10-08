from __future__ import annotations
import json
import sqlite3
from uuid import NAMESPACE_URL, uuid5
from hyprial.daemon import Alarm
from hyprial.daemon.impl.inbox.contracts.api  import (
    DeliveryLifecycle,
    InboxMessage,
    OutboxItem,
)
from hyprial.daemon.impl.inbox.tracking.progress  import (
    PROGRESS_INTENT,
    ProgressEvent,
    decode_progress_event,
    encode_progress_event,
)
from hyprial.daemon.impl.inbox.service.synchronization import _synchronized

"""SQLite-backed outbox, inbox, deduplication, FIFO, DLQ and custody state."""
# Route C progress-event storage bounds (backpressure layer 3).  The
# system_notices table predates these guards and stays unbounded; the
# progress table never repeats that.
PROGRESS_EVENT_TTL_MS = 600_000  # 10 minutes
PROGRESS_EVENT_PER_DELIVERY_LIMIT = 50
PROGRESS_EVENT_PER_RECIPIENT_LIMIT = 500

class InboxServiceOutboxMixin:
    def _claim_alarm(
        self,
        conversation_id: str,
        reason: str,
        window_start_ms: int,
        claimed_at_ms: int,
    ) -> bool:
        with self._db:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO alarm_throttle(
                       conversation_id, reason, window_start_ms, claimed_at_ms
                   ) VALUES (?, ?, ?, ?)""",
                (conversation_id, reason, window_start_ms, claimed_at_ms),
            )
            self._db.execute(
                "DELETE FROM alarm_throttle WHERE window_start_ms < ?",
                (window_start_ms - 3_600_000,),
            )
        return cursor.rowcount == 1
    def _deliver_system_notice(self, alarm: Alarm, text: str) -> bool:
        notice_id = str(
            uuid5(
                NAMESPACE_URL,
                f"hyprial:alarm:{alarm.message_id}:{alarm.reason}:{alarm.sender}",
            )
        )
        payload = json.dumps(
            {
                "message": text,
                "originalMessageId": alarm.message_id,
                "reason": alarm.reason,
                "systemNotice": True,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        notice = InboxMessage(
            message_id=notice_id,
            conversation_id=alarm.conversation_id,
            sender=f"system:hyprial:{self.node_id}",
            recipient=alarm.sender,
            payload=payload,
            intent="system",
            lifecycle=DeliveryLifecycle.ONLINE_ONLY,
            idempotency_key=f"alarm:{alarm.message_id}:{alarm.reason}",
            created_at_ms=self._now_ms(),
        )
        sender_node = self._agent_node(alarm.sender)
        if alarm.audience == "operator":
            # Local operator notice keyed by this node's own id (bare): the
            # operator reads it back via system_notices(node_id).  This is not
            # a user-typed address and must not be rejected as an unrouteable
            # bare name (2026-09-14 defect class A is scoped to user-typed
            # escalate_to / report_to addresses, not the daemon's own operator
            # notice).
            return self.receive_system_notice(notice)
        if sender_node is None:
            # A bare name is not an address: no reader exists for a notice
            # keyed by it (2026-09-14 defect class A).  Returning False makes
            # the emitter record ``alarm.failed`` instead of pretending a
            # local delivery happened.  No notice row is written.
            return False
        if sender_node == self.node_id:
            return self.receive_system_notice(notice)
        return self._transport.deliver_notice(sender_node, notice)
    @staticmethod
    def _agent_node(sender: str) -> str | None:
        from hyprial.kernel import parse_agent_uri

        parsed = parse_agent_uri(sender)
        return parsed[1] if parsed is not None else None
    @_synchronized
    def receive_system_notice(self, notice: InboxMessage) -> bool:
        """Persist an offered system notice outside inbox FIFO and receipts."""

        if notice.intent != "system":
            return False
        with self._db:
            cursor = self._persist_system_notice_locked(notice)
        return cursor.rowcount == 1 or self._has_system_notice(notice.message_id)
    def _persist_system_notice_locked(
        self, notice: InboxMessage
    ) -> sqlite3.Cursor:
        if notice.intent != "system":
            raise ValueError("system notice intent must be 'system'")
        return self._db.execute(
            """INSERT OR IGNORE INTO system_notices(
                   message_id, conversation_id, sender, recipient, payload,
                   intent, lifecycle, idempotency_key, created_at_ms
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                notice.message_id,
                notice.conversation_id,
                notice.sender,
                notice.recipient,
                notice.payload,
                notice.intent,
                notice.lifecycle.value,
                notice.idempotency_key,
                notice.created_at_ms,
            ),
        )
    def _has_system_notice(self, message_id: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM system_notices WHERE message_id = ?", (message_id,)
            ).fetchone()
            is not None
        )
    @_synchronized
    def system_notices(self, recipient: str) -> tuple[InboxMessage, ...]:
        rows = self._db.execute(
            """SELECT * FROM system_notices WHERE recipient = ?
               ORDER BY created_at_ms, rowid""",
            (recipient,),
        ).fetchall()
        return tuple(self._row_message(row) for row in rows)
    @_synchronized
    def drain_system_notices(self, recipient: str) -> tuple[InboxMessage, ...]:
        notices = self.system_notices(recipient)
        if notices:
            with self._db:
                self._db.executemany(
                    "DELETE FROM system_notices WHERE message_id = ?",
                    ((item.message_id,) for item in notices),
                )
        return notices
    @_synchronized
    def dismiss_system_notice(self, message_id: str) -> bool:
        with self._db:
            cursor = self._db.execute(
                "DELETE FROM system_notices WHERE message_id = ?", (message_id,)
            )
        return cursor.rowcount == 1
    @_synchronized
    def submit_progress_event(self, event: ProgressEvent, *, recipient: str) -> bool:
        """Offer one coalesced progress event to the original sender's daemon.

        Routing deliberately mirrors system notices without sharing their
        prompt-dispatch table: canonical remote agents go out on the progress
        keyspace; every other sender shape degrades to local storage (the v1
        addressing boundary documented for route C).
        """

        payload = encode_progress_event(event)
        message = InboxMessage(
            message_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"hyprial-progress:{event.delivery_id}:{event.seq}:{event.phase}",
                )
            ),
            conversation_id=event.conversation_id,
            sender=event.actor,
            recipient=recipient,
            payload=payload,
            intent=PROGRESS_INTENT,
            lifecycle=DeliveryLifecycle.ONLINE_ONLY,
            idempotency_key=f"progress:{event.delivery_id}:{event.seq}",
            created_at_ms=event.emitted_at_ms,
        )
        recipient_node = self._agent_node(recipient)
        if recipient_node is None or recipient_node == self.node_id:
            return self.receive_progress_event(message)
        deliver = getattr(self._transport, "deliver_progress", None)
        return callable(deliver) and bool(deliver(recipient_node, message))
    @_synchronized
    def receive_progress_event(self, message: InboxMessage) -> bool:
        """Persist one offered progress event, outside FIFO and receipts.

        Guards the exact ``progress`` intent and a contract-valid payload
        (anything else is not ours); then applies the table's bounds: TTL
        sweep, per-delivery cap, per-recipient cap.  Loss by trimming is
        sanctioned -- every retained event stays self-contained, and the
        consumer reads ``droppedSinceSeq`` for the accounting.
        """

        if message.intent != PROGRESS_INTENT:
            return False
        event = decode_progress_event(message.payload)
        if event is None:
            return False
        now = self._now_ms()
        with self._db:
            self._db.execute(
                "DELETE FROM progress_events WHERE expires_at_ms <= ?", (now,)
            )
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO progress_events(
                       message_id, delivery_id, conversation_id, sender,
                       recipient, payload, intent, lifecycle,
                       idempotency_key, created_at_ms, expires_at_ms
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message.message_id,
                    event.delivery_id,
                    message.conversation_id,
                    message.sender,
                    message.recipient,
                    message.payload,
                    message.intent,
                    message.lifecycle.value,
                    message.idempotency_key,
                    message.created_at_ms,
                    now + PROGRESS_EVENT_TTL_MS,
                ),
            )
            if cursor.rowcount == 1:
                # Keep the NEWEST rows: trim by exclusion from a
                # most-recent-first window, rowid breaking timestamp ties in
                # arrival order.
                self._db.execute(
                    """DELETE FROM progress_events
                       WHERE recipient = ? AND delivery_id = ?
                       AND message_id NOT IN (
                           SELECT message_id FROM progress_events
                           WHERE recipient = ? AND delivery_id = ?
                           ORDER BY created_at_ms DESC, rowid DESC LIMIT ?)""",
                    (
                        message.recipient,
                        event.delivery_id,
                        message.recipient,
                        event.delivery_id,
                        PROGRESS_EVENT_PER_DELIVERY_LIMIT,
                    ),
                )
                self._db.execute(
                    """DELETE FROM progress_events
                       WHERE recipient = ?
                       AND message_id NOT IN (
                           SELECT message_id FROM progress_events
                           WHERE recipient = ?
                           ORDER BY created_at_ms DESC, rowid DESC LIMIT ?)""",
                    (
                        message.recipient,
                        message.recipient,
                        PROGRESS_EVENT_PER_RECIPIENT_LIMIT,
                    ),
                )
        return cursor.rowcount == 1 or self._has_progress_event(
            message.message_id
        )
    def _has_progress_event(self, message_id: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM progress_events WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            is not None
        )
    @_synchronized
    def list_progress_events(
        self, recipient: str, *, delivery_id: str | None = None
    ) -> tuple[InboxMessage, ...]:
        """Non-destructive read; cleanup is TTL-driven, not read-driven.

        ``sinceSeq`` filtering happens one layer up, where payloads are
        decoded (the IPC method); storage stays payload-agnostic beyond the
        contract validation at receive time.
        """

        if delivery_id is None:
            rows = self._db.execute(
                """SELECT * FROM progress_events WHERE recipient = ?
                   ORDER BY created_at_ms, rowid""",
                (recipient,),
            ).fetchall()
        else:
            rows = self._db.execute(
                """SELECT * FROM progress_events
                   WHERE recipient = ? AND delivery_id = ?
                   ORDER BY created_at_ms, rowid""",
                (recipient, delivery_id),
            ).fetchall()
        return tuple(self._row_message(row) for row in rows)
    @_synchronized
    def outbox_count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
    @_synchronized
    def outbox_items(self) -> tuple[OutboxItem, ...]:
        rows = self._db.execute(
            "SELECT * FROM outbox ORDER BY created_at_ms, rowid"
        ).fetchall()
        return tuple(
            OutboxItem(
                self._row_message(row),
                attempts=int(row["attempts"]),
                next_attempt_ms=int(row["next_attempt_ms"]),
                expires_at_ms=int(row["expires_at_ms"]),
                retry_started_at_ms=(
                    int(row["retry_started_at_ms"])
                    if row["retry_started_at_ms"] is not None
                    else None
                ),
                confirmation_started_at_ms=(
                    int(row["confirmation_started_at_ms"])
                    if row["confirmation_started_at_ms"] is not None
                    else None
                ),
                confirmation_attempts=int(row["confirmation_attempts"]),
            )
            for row in rows
        )
