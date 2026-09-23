"""SQLite-backed outbox, inbox, deduplication, FIFO, DLQ and custody state."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Any, Self, TypeVar
from uuid import NAMESPACE_URL, uuid5

from hyprial.alarm import Alarm, AlarmDelivery, AlarmEmitter, audience_for_sender
from hyprial.contracts import ipc_errors
from hyprial.log import Logger

from .api import (
    AckResult,
    DeliveryLifecycle,
    DeliveryTransport,
    FailureResult,
    HarnessFailureAttempt,
    HarnessFailureSettlement,
    InboxMessage,
    InboxPruneItem,
    OutboxItem,
    OutboxPruneItem,
    ReceiveResult,
    SubmissionResult,
)
from .pull import (
    DEFAULT_HOLD_TTL_MS,
    DeliveryStatus,
    DeliveryStatusStore,
    HoldPolicy,
    HoldReason,
    TerminalState,
)
from .progress import (
    PROGRESS_INTENT,
    ProgressEvent,
    decode_progress_event,
    encode_progress_event,
)

DAY_MS = 86_400_000

# Route C progress-event storage bounds (backpressure layer 3).  The
# system_notices table predates these guards and stays unbounded; the
# progress table never repeats that.
PROGRESS_EVENT_TTL_MS = 600_000  # 10 minutes
PROGRESS_EVENT_PER_DELIVERY_LIMIT = 50
PROGRESS_EVENT_PER_RECIPIENT_LIMIT = 500

_R = TypeVar("_R")


class _ActorMutationContext:
    """No-op guard used when an actor already owns mutation serialization.

    SQLite transaction blocks remain unchanged.  This context only replaces
    the legacy service-level business lock; it is deliberately private so a
    caller cannot opt out of serialization without going through the actor
    coordinator.
    """

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> None:
        return None


def _synchronized(method: Callable[..., _R]) -> Callable[..., _R]:  # noqa: UP047
    @wraps(method)
    def locked(self: InboxService, *args: Any, **kwargs: Any) -> _R:
        with self._lock:
            return method(self, *args, **kwargs)

    return locked


class ConsumptionState(StrEnum):
    """Where one dispatched message stands in its recipient's consumption.

    The PAC workflow executor's ack-kind await reads this: a message the
    target consumed proves receipt-of-work; expiry is an observable terminal
    record, not silence (design-pac-workflow §4.3).
    """

    PENDING = "pending"      # inbox row present, unconsumed
    CONSUMED = "consumed"    # inbox row consumed, or pruned with a FETCHED record
    FAILED = "failed"        # retained row has a durable terminal failure tombstone
    EXPIRED = "expired"      # terminal record says expired (TTL eviction)
    UNKNOWN = "unknown"      # no row and no record (never seen, or purged)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    backoff_seconds: tuple[int, ...] = (1, 5, 30, 120, 600)
    interactive_backoff_seconds: tuple[int, ...] = (1, 5, 30, 120, 600, 1800)

    @property
    def maximum_attempts(self) -> int:
        """The legacy/headless retry budget kept for wire compatibility."""

        return len(self.backoff_seconds)

    def backoff_seconds_for(self, *, interactive: bool) -> tuple[int, ...]:
        return self.interactive_backoff_seconds if interactive else self.backoff_seconds

    def maximum_attempts_for(self, *, interactive: bool) -> int:
        return len(self.backoff_seconds_for(interactive=interactive))


def _bare_sender(sender: str) -> bool:
    """Is this sender a bare (node-shaped) name the ingress gate must refuse?

    The classifier policy remains with daemon identity; its target-kind
    vocabulary comes from the dependency-free URI leaf module.
    """

    from hyprial.daemon.identity import classify_target_identity
    from hyprial.uri import TARGET_KIND_HOST

    return classify_target_identity(sender) == TARGET_KIND_HOST


class InboxService:
    dedup_window_ms = DAY_MS
    # Held-message TTL, both as sender-outbox and as mailbox custody.  Was
    # 30 days / 7 days; the delivery design (section 6) cut both to 30 minutes
    # on the reasoning that a message nobody collected in that window is
    # probably stale anyway -- acceptable only because eviction now writes an
    # observable ``expired`` record instead of dropping the message silently.
    durable_ttl_ms = DEFAULT_HOLD_TTL_MS
    custody_ttl_ms = DEFAULT_HOLD_TTL_MS

    def __init__(
        self,
        database: Path,
        transport: DeliveryTransport,
        *,
        retry_policy: RetryPolicy | None = None,
        max_inbox_items: int = 10_000,
        max_custody_bytes: int = 1 << 30,
        node_id: str = "local",
        hold_policy: HoldPolicy | None = None,
        logger: Logger | None = None,
        alarm_human_delivery: AlarmDelivery | None = None,
        interactive_recipient: Callable[[str], bool] | None = None,
        _serialized_by_actor: bool = False,
    ) -> None:
        database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = (
            _ActorMutationContext() if _serialized_by_actor else threading.RLock()
        )
        self._db = sqlite3.connect(database, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._transport = transport
        self.retry_policy = retry_policy or RetryPolicy()
        self.max_inbox_items = max_inbox_items
        self.max_custody_bytes = max_custody_bytes
        self.node_id = node_id
        self.hold_policy = hold_policy or HoldPolicy()
        self._interactive_recipient = interactive_recipient or (
            lambda _recipient: False
        )
        # Per-instance so a daemon or agent can be configured independently
        # without mutating the class default other instances read.
        self.durable_ttl_ms = self.hold_policy.ttl_ms
        self.custody_ttl_ms = self.hold_policy.ttl_ms
        self._create_schema()
        self._status = DeliveryStatusStore(
            self._db,
            lock=self._lock,
            retention_ms=self.hold_policy.status_retention_ms,
        )
        self._logger = logger or Logger.daemon(database.parent, name=node_id)
        self._alarm = AlarmEmitter(
            self._logger,
            deliver_human=alarm_human_delivery,
            deliver_agent=self._deliver_system_notice,
            claim=self._claim_alarm,
        )

    def _create_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS outbox (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                payload BLOB NOT NULL,
                intent TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                idempotency_key TEXT,
                created_at_ms INTEGER NOT NULL,
                expires_at_ms INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_ms INTEGER NOT NULL,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS outbox_due ON outbox(next_attempt_ms, created_at_ms);

            CREATE TABLE IF NOT EXISTS inbox (
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
                expires_at_ms INTEGER,
                consumed INTEGER NOT NULL DEFAULT 0,
                acknowledged_at_ms INTEGER,
                fetched_at_ms INTEGER,
                origin_node TEXT
            );
            CREATE INDEX IF NOT EXISTS inbox_fifo ON inbox(recipient, consumed, arrival_id);

            CREATE TABLE IF NOT EXISTS harness_failure_settlements (
                message_id TEXT PRIMARY KEY,
                recipient TEXT NOT NULL,
                cycle INTEGER NOT NULL,
                failure_code TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                max_attempts INTEGER NOT NULL,
                next_attempt_ms INTEGER,
                terminal INTEGER NOT NULL,
                permanent INTEGER NOT NULL,
                terminal_reason TEXT,
                first_failed_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                terminal_at_ms INTEGER
            );
            CREATE INDEX IF NOT EXISTS harness_failure_due
                ON harness_failure_settlements(terminal, next_attempt_ms);

            CREATE TABLE IF NOT EXISTS harness_failure_attempts (
                message_id TEXT NOT NULL,
                cycle INTEGER NOT NULL,
                attempt INTEGER NOT NULL,
                failure_code TEXT NOT NULL,
                permanent INTEGER NOT NULL,
                failed_at_ms INTEGER NOT NULL,
                PRIMARY KEY(message_id, cycle, attempt)
            );

            CREATE TABLE IF NOT EXISTS dedup (
                dedup_key TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                expires_at_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS dedup_expiry ON dedup(expires_at_ms);

            CREATE TABLE IF NOT EXISTS dlq (
                dlq_id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                payload BLOB NOT NULL,
                reason TEXT NOT NULL,
                failed_at_ms INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS custody (
                message_id TEXT PRIMARY KEY,
                mailbox_node TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                payload BLOB NOT NULL,
                intent TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                idempotency_key TEXT,
                created_at_ms INTEGER NOT NULL,
                accepted_at_ms INTEGER NOT NULL,
                expires_at_ms INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS custody_due ON custody(next_attempt_ms, accepted_at_ms);

            CREATE TABLE IF NOT EXISTS system_notices (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                payload BLOB NOT NULL,
                intent TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                idempotency_key TEXT,
                created_at_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS system_notices_recipient
                ON system_notices(recipient, created_at_ms);

            -- Route C progress events: a table of their own, so the notice
            -- dispatch path (which turns a stored notice into a harness
            -- PROMPT) structurally cannot read them.  Unlike system_notices
            -- this table is bounded: per-delivery and per-recipient caps
            -- plus an expires_at_ms TTL swept on every receive.
            CREATE TABLE IF NOT EXISTS progress_events (
                message_id TEXT PRIMARY KEY,
                delivery_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                payload BLOB NOT NULL,
                intent TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                idempotency_key TEXT,
                created_at_ms INTEGER NOT NULL,
                expires_at_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS progress_events_recipient
                ON progress_events(recipient, delivery_id, created_at_ms);

            -- D22 actor-effect settlement ledger.  The legacy InboxService
            -- does not read it, but creating the table here keeps one schema
            -- for actor and compatibility constructors over the same file.
            CREATE TABLE IF NOT EXISTS actor_submission_receipts (
                correlation_id TEXT PRIMARY KEY,
                command_digest TEXT NOT NULL,
                message_id TEXT NOT NULL,
                accepted INTEGER NOT NULL,
                queued INTEGER NOT NULL,
                code TEXT,
                custody_mailbox TEXT,
                recorded_at_ms INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS alarm_throttle (
                conversation_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                window_start_ms INTEGER NOT NULL,
                claimed_at_ms INTEGER NOT NULL,
                PRIMARY KEY (conversation_id, reason, window_start_ms)
            );
            """
        )
        inbox_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(inbox)").fetchall()
        }
        if "fetched_at_ms" not in inbox_columns:
            self._db.execute("ALTER TABLE inbox ADD COLUMN fetched_at_ms INTEGER")
        if "origin_node" not in inbox_columns:
            # Transport-layer provenance, added after the fact: pre-existing
            # rows keep NULL (their physical origin was never recorded —
            # exactly the forensic gap this column closes going forward).
            self._db.execute("ALTER TABLE inbox ADD COLUMN origin_node TEXT")
        if "expires_at_ms" not in inbox_columns:
            # TTL deadline for unconsumed rows: orphaned or never-consumed
            # messages used to accumulate forever (a recipient URI mismatch
            # after a node rename left rows behind every re-addressed send).
            # Backfill from THIS node's own clock -- ``received_at_ms`` is
            # stamped here when the message was taken, so ``received_at_ms +
            # ttl`` is the same holder-clock arithmetic new rows get, never
            # the sender's foreign ``created_at_ms`` (section 6).
            self._db.execute("ALTER TABLE inbox ADD COLUMN expires_at_ms INTEGER")
            self._db.execute(
                "UPDATE inbox SET expires_at_ms = received_at_ms + ? "
                "WHERE expires_at_ms IS NULL",
                (self.durable_ttl_ms,),
            )
        settlement_columns = {
            str(row["name"])
            for row in self._db.execute(
                "PRAGMA table_info(harness_failure_settlements)"
            ).fetchall()
        }
        if "cycle" not in settlement_columns:
            self._db.execute(
                "ALTER TABLE harness_failure_settlements "
                "ADD COLUMN cycle INTEGER NOT NULL DEFAULT 1"
            )
        if "terminal_reason" not in settlement_columns:
            self._db.execute(
                "ALTER TABLE harness_failure_settlements "
                "ADD COLUMN terminal_reason TEXT"
            )
        if "first_failed_at_ms" not in settlement_columns:
            self._db.execute(
                "ALTER TABLE harness_failure_settlements "
                "ADD COLUMN first_failed_at_ms INTEGER NOT NULL DEFAULT 0"
            )
            self._db.execute(
                "UPDATE harness_failure_settlements "
                "SET first_failed_at_ms = updated_at_ms"
            )
        if "terminal_at_ms" not in settlement_columns:
            self._db.execute(
                "ALTER TABLE harness_failure_settlements "
                "ADD COLUMN terminal_at_ms INTEGER"
            )
            self._db.execute(
                "UPDATE harness_failure_settlements "
                "SET terminal_at_ms = updated_at_ms WHERE terminal = 1"
            )
        attempt_columns = {
            str(row["name"])
            for row in self._db.execute(
                "PRAGMA table_info(harness_failure_attempts)"
            ).fetchall()
        }
        if "cycle" not in attempt_columns:
            self._db.executescript(
                """
                ALTER TABLE harness_failure_attempts
                    RENAME TO harness_failure_attempts_legacy;
                CREATE TABLE harness_failure_attempts (
                    message_id TEXT NOT NULL,
                    cycle INTEGER NOT NULL,
                    attempt INTEGER NOT NULL,
                    failure_code TEXT NOT NULL,
                    permanent INTEGER NOT NULL,
                    failed_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(message_id, cycle, attempt)
                );
                INSERT INTO harness_failure_attempts (
                    message_id, cycle, attempt, failure_code, permanent,
                    failed_at_ms
                )
                SELECT message_id, 1, attempt, failure_code, permanent,
                       failed_at_ms
                  FROM harness_failure_attempts_legacy;
                DROP TABLE harness_failure_attempts_legacy;
                """
            )
        # Created outside the executescript: on a legacy database the column
        # does not exist until the ALTER above runs, so the index has to come
        # after it.  ``IF NOT EXISTS`` keeps it a no-op on reopens.
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS inbox_expiry ON inbox(consumed, expires_at_ms)"
        )
        self._db.commit()

    @staticmethod
    def _now_ms() -> int:
        return time.time_ns() // 1_000_000

    @_synchronized
    def submit(
        self, message: InboxMessage, *, now_ms: int | None = None, defer_direct: bool = False
    ) -> SubmissionResult:
        now = self._now_ms() if now_ms is None else now_ms
        if (
            message.lifecycle == DeliveryLifecycle.ONLINE_ONLY
            and not self._transport.is_online(message.recipient)
        ):
            return SubmissionResult(
                message_id=message.message_id,
                accepted=False,
                code="TARGET_OFFLINE",
            )

        self._insert_outbox(message, now)
        if defer_direct:
            # The PAC workflow tick's required shape (#99 guardrail): inside
            # the IPC request lock the caller only enqueues and advances
            # state — direct delivery and custody transfer are BLOCKING
            # network calls and must stay on the existing retry pump
            # (retry_due), which owns all network I/O on this cadence.
            self._defer_outbox(message, now)
            return SubmissionResult(message.message_id, accepted=True, queued=True)
        if self._transport.is_online(message.recipient) and self._attempt_direct(
            message, now
        ):
            return SubmissionResult(message.message_id, accepted=True, queued=False)

        mailbox = self._attempt_custody(message)
        if mailbox is not None:
            self._delete_outbox(message.message_id)
            return SubmissionResult(
                message.message_id,
                accepted=True,
                queued=False,
                custody_mailbox=mailbox,
            )
        if not self._transport.is_online(message.recipient):
            self._defer_outbox(message, now)
        return SubmissionResult(message.message_id, accepted=True, queued=True)

    def _insert_outbox(self, message: InboxMessage, now_ms: int) -> None:
        self._db.execute(
            """INSERT OR IGNORE INTO outbox (
                   message_id, conversation_id, sender, recipient, payload, intent,
                   lifecycle, idempotency_key, created_at_ms, expires_at_ms, next_attempt_ms
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message.message_id,
                message.conversation_id,
                message.sender,
                message.recipient,
                message.payload,
                message.intent,
                message.lifecycle.value,
                message.idempotency_key,
                message.created_at_ms,
                # The hold deadline is this holder's own clock plus the TTL,
                # stamped when it took the message -- never derived from
                # ``message.created_at_ms``, which came off another machine's
                # clock (section 6).
                now_ms + self.durable_ttl_ms,
                now_ms,
            ),
        )
        self._db.commit()
        self._enforce_hold_capacity("outbox", now_ms)

    def _attempt_direct(self, message: InboxMessage, now_ms: int) -> bool:
        if self._transport.deliver(message):
            # A confirmed receipt means the recipient committed the message,
            # so the holder may release it and mirror the ``fetched`` verdict
            # locally.  The mirror is a latency optimisation only: it lets the
            # sender answer "what happened to it" without a mesh round trip.
            # The recipient's own record stays authoritative -- this one
            # inherits the receipt leg's trust model (defect #9: the receipt
            # queryable does not check who is answering) -- and
            # ``pull._precedence`` enforces that by evidence class.
            #
            # Stamped from a clock read taken HERE, after the receipt.
            # ``now_ms`` was read by ``submit`` before ``deliver`` blocked on
            # a network round trip, so using it dated the record earlier than
            # the delivery it describes.
            confirmed_at_ms = self._now_ms()
            with self._db:
                self._record_terminal(
                    message,
                    state=TerminalState.FETCHED,
                    reason=HoldReason.RECEIPT_CONFIRMED,
                    now_ms=confirmed_at_ms,
                )
                self._db.execute(
                    "DELETE FROM outbox WHERE message_id = ?", (message.message_id,)
                )
            return True
        row = self._db.execute(
            "SELECT attempts FROM outbox WHERE message_id = ?", (message.message_id,)
        ).fetchone()
        if row is not None:
            attempts = int(row["attempts"]) + 1
            schedule = self._retry_schedule(message.recipient)
            delay = schedule[min(attempts - 1, len(schedule) - 1)]
            self._db.execute(
                """UPDATE outbox
                      SET attempts = ?,
                          next_attempt_ms = MIN(expires_at_ms, ?),
                          last_error = ?
                    WHERE message_id = ?""",
                (
                    attempts,
                    now_ms + delay * 1000,
                    "receipt not confirmed",
                    message.message_id,
                ),
            )
            self._db.commit()
            self._log_retry(
                message,
                attempts,
                now_ms + delay * 1000,
                "receipt not confirmed",
            )
        return False

    def _attempt_custody(self, message: InboxMessage) -> str | None:
        for mailbox in self._transport.online_mailboxes():
            if self._transport.transfer_custody(mailbox, message):
                return mailbox
        return None

    def _retry_schedule(self, recipient: str) -> tuple[int, ...]:
        return self.retry_policy.backoff_seconds_for(
            interactive=self._interactive_recipient(recipient)
        )

    def _defer_outbox(self, message: InboxMessage, now_ms: int, *, count_attempt: bool = False) -> None:
        if count_attempt:
            row = self._db.execute(
                "SELECT attempts FROM outbox WHERE message_id = ?", (message.message_id,)
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"]) + 1
            schedule = self._retry_schedule(message.recipient)
            delay = schedule[min(attempts - 1, len(schedule) - 1)]
            with self._db:
                self._db.execute(
                    """UPDATE outbox
                      SET attempts = ?,
                          next_attempt_ms = MIN(expires_at_ms, ?),
                          last_error = ?
                    WHERE message_id = ?""",
                    (attempts, now_ms + delay * 1000, "recipient offline", message.message_id),
                )
            self._log_retry(
                message, attempts, now_ms + delay * 1000, "recipient offline"
            )
            return
        with self._db:
            self._db.execute(
                """UPDATE outbox
                      SET next_attempt_ms = MIN(expires_at_ms, ?)
                    WHERE message_id = ?""",
                (
                    now_ms + self._retry_schedule(message.recipient)[0] * 1000,
                    message.message_id,
                ),
            )

    @_synchronized
    def retry_due(self, *, now_ms: int | None = None) -> list[SubmissionResult]:
        now = self._now_ms() if now_ms is None else now_ms
        # The delivery pump is the only periodic tick this layer has, so the
        # terminal-record ledger is trimmed here.  Retention is far longer than
        # the message TTL: a sender that was offline when its message ended
        # must still be able to pull the verdict when it comes back.
        with self._db:
            self._status.purge(now_ms=now)
        rows = self._db.execute(
            "SELECT * FROM outbox WHERE next_attempt_ms <= ? ORDER BY created_at_ms, rowid",
            (now,),
        ).fetchall()
        outcomes: list[SubmissionResult] = []
        for row in rows:
            message = self._row_message(row)
            expired = now >= int(row["expires_at_ms"])
            maximum_attempts = self.retry_policy.maximum_attempts_for(
                interactive=self._interactive_recipient(message.recipient)
            )
            exhausted = int(row["attempts"]) >= maximum_attempts
            if expired or exhausted:
                # A sender that was offline during the one-shot fetch receipt
                # gets one durable query before terminal DLQ. Ordinary retries
                # stay non-blocking and do not multiply receipt timeouts by N.
                confirm_fetch = getattr(self._transport, "confirm_fetch", None)
                if callable(confirm_fetch) and confirm_fetch(message):
                    self.retire_outbox_receipt(
                        message.sender, message.message_id, now_ms=now
                    )
                    outcomes.append(
                        SubmissionResult(message.message_id, True, queued=False)
                    )
                    continue
            if expired:
                self._outbox_to_dlq(row, "DELIVERY_EXPIRED", now)
                outcomes.append(
                    SubmissionResult(message.message_id, False, code="DELIVERY_EXPIRED")
                )
                continue
            if exhausted:
                self._outbox_to_dlq(row, "DELIVERY_RETRY_EXHAUSTED", now)
                outcomes.append(
                    SubmissionResult(
                        message.message_id, False, code="DELIVERY_RETRY_EXHAUSTED"
                    )
                )
                continue
            delivered = False
            if self._transport.is_online(message.recipient):
                delivered = self._attempt_direct(message, now)
            if delivered:
                outcomes.append(
                    SubmissionResult(message.message_id, True, queued=False)
                )
                continue
            mailbox = self._attempt_custody(message)
            if mailbox is not None:
                self._delete_outbox(message.message_id)
                outcomes.append(
                    SubmissionResult(
                        message.message_id,
                        True,
                        queued=False,
                        custody_mailbox=mailbox,
                    )
                )
            else:
                if not self._transport.is_online(message.recipient):
                    # C2 (2026-08-22 outbox dead-letter audit): an offline
                    # deferral IS a retry attempt — before this, only failed
                    # deliveries incremented `attempts`, so an always-offline
                    # recipient retried for the whole TTL (weeks) with the
                    # mailbox query fired every deferral.  One budget covers
                    # both failure modes now: `attempts` counts retry CYCLES,
                    # not just wire attempts, and deferrals ride the same
                    # backoff progression.  This cap is per-MESSAGE outbox
                    # retries — a different thing from PAC's max_attempts
                    # (per-run) and routine's SOURCE_ERROR_CAP (source
                    # queries); keep the names apart.
                    self._defer_outbox(message, now, count_attempt=True)
                outcomes.append(SubmissionResult(message.message_id, True, queued=True))
        outcomes.extend(self.retry_custody_due(now_ms=now))
        return outcomes

    @_synchronized
    def receive(
        self, message: InboxMessage, *, now_ms: int | None = None
    ) -> ReceiveResult:
        now = self._now_ms() if now_ms is None else now_ms
        if _bare_sender(message.sender):
            # Ingress admission gate (Allen B): after the daemon send
            # boundary canonicalizes every sender, a bare (node-shaped)
            # sender on the wire means a pre-gate or bypassing client.
            # Refuse to commit — no row, so no receipt is ever signed — and
            # tell the origin why.  Never silent on either side: the
            # sender's unreceipted retries exhaust into its own DLQ/alarm,
            # and the notice states the reason.
            return self._reject_bare_sender(message, now)
        with self._db:
            self._db.execute("DELETE FROM dedup WHERE expires_at_ms <= ?", (now,))
            keys = [f"msgid:{message.message_id}"]
            if message.idempotency_key:
                keys.append(f"idempotency:{message.idempotency_key}")
            placeholders = ",".join("?" for _ in keys)
            duplicate = self._db.execute(
                f"SELECT 1 FROM dedup WHERE dedup_key IN ({placeholders}) LIMIT 1", keys
            ).fetchone()
            if duplicate is not None:
                # Persist an alias tombstone so a distinct msgid carrying an
                # already-seen idempotency key can still receive a durable ACK.
                self._db.execute(
                    """INSERT OR REPLACE INTO dedup(dedup_key, message_id, expires_at_ms)
                       VALUES (?, ?, ?)""",
                    (
                        f"msgid:{message.message_id}",
                        message.message_id,
                        now + self.dedup_window_ms,
                    ),
                )
                # A deduplicated retry did reach its recipient -- the payload is
                # already here under the original message id.  Recording it
                # ``fetched`` is what makes retrying safe to observe: the sender
                # sees delivery, not a message that vanished into the dedup
                # table.
                self._record_terminal(
                    message,
                    state=TerminalState.FETCHED,
                    reason=HoldReason.RECIPIENT_DUPLICATE,
                    now_ms=now,
                )
                return ReceiveResult(
                    message.message_id,
                    accepted=False,
                    acknowledged=True,
                    duplicate=True,
                )
            terminal_duplicate = self._db.execute(
                """SELECT 1 FROM harness_failure_settlements
                   WHERE message_id = ? AND terminal = 1""",
                (message.message_id,),
            ).fetchone()
            if terminal_duplicate is not None:
                return ReceiveResult(
                    message.message_id,
                    accepted=False,
                    acknowledged=True,
                    duplicate=True,
                )
            pending = self._db.execute(
                """SELECT COUNT(*) FROM inbox
                   WHERE consumed = 0
                     AND NOT EXISTS (
                         SELECT 1 FROM harness_failure_settlements
                          WHERE harness_failure_settlements.message_id = inbox.message_id
                            AND harness_failure_settlements.terminal = 1
                     )"""
            ).fetchone()[0]
            if int(pending) >= self.max_inbox_items:
                return ReceiveResult(
                    message.message_id,
                    accepted=False,
                    acknowledged=False,
                    code="DELIVERY_REJECTED",
                )
            # An acknowledged item may reuse its msgid after the tombstone window.
            # A completed transient-failure cycle keeps its append-only attempt
            # evidence, but its budget must not leak into this new logical use.
            self._db.execute(
                """DELETE FROM harness_failure_settlements
                   WHERE message_id = ? AND terminal = 0
                     AND EXISTS (
                         SELECT 1 FROM inbox
                          WHERE inbox.message_id = ? AND inbox.consumed = 1
                     )""",
                (message.message_id, message.message_id),
            )
            self._db.execute(
                "DELETE FROM inbox WHERE message_id = ? AND consumed = 1",
                (message.message_id,),
            )
            self._db.execute(
                """INSERT INTO inbox (
                       message_id, conversation_id, sender, recipient, payload, intent,
                       lifecycle, idempotency_key, created_at_ms, received_at_ms,
                       expires_at_ms, origin_node
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message.message_id,
                    message.conversation_id,
                    message.sender,
                    message.recipient,
                    message.payload,
                    message.intent,
                    message.lifecycle.value,
                    message.idempotency_key,
                    message.created_at_ms,
                    now,
                    # TTL deadline counted from this node's own clock at
                    # receive time (section 6): a message nobody consumed
                    # within the hold window is stale, same as an outbox
                    # hold nobody collected.
                    now + self.durable_ttl_ms,
                    message.origin_node,
                ),
            )
            for key in keys:
                self._db.execute(
                    "INSERT INTO dedup(dedup_key, message_id, expires_at_ms) VALUES (?, ?, ?)",
                    (key, message.message_id, now + self.dedup_window_ms),
                )
            self._record_terminal(
                message,
                state=TerminalState.FETCHED,
                reason=HoldReason.RECIPIENT_COMMIT,
                now_ms=now,
            )
        wake = message.intent in {"request", "reply"}
        return ReceiveResult(
            message.message_id,
            accepted=True,
            acknowledged=True,
            wake=wake,
            auto_reply=message.intent == "request",
        )

    def _reject_bare_sender(
        self, message: InboxMessage, now_ms: int
    ) -> ReceiveResult:
        """Refuse a bare-sender message: no commit, a notice states why.

        The notice id derives deterministically from the rejected message
        id, so the sender's unreceipted retries dedup at the receiving node
        (INSERT OR IGNORE) instead of piling up.  Delivery target: the
        transport-stamped origin node when present; a pre-stamp peer's bare
        sender IS its node id, so an online bare sender is the fallback.
        When neither is reachable the notice is stored locally — the same
        pattern the alarm path follows for an unplaceable sender.
        """

        notice_id = str(
            uuid5(NAMESPACE_URL, f"hyprial:sender-rejected:{message.message_id}")
        )
        payload = json.dumps(
            {
                "message": (
                    f"message {message.message_id} was rejected at ingress: "
                    f"sender {message.sender!r} is a bare node-shaped name, "
                    "not a canonical agent:<owner>:<machine>:<actor> identity"
                ),
                "originalMessageId": message.message_id,
                "reason": "SENDER_NOT_CANONICAL",
                "systemNotice": True,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        notice = InboxMessage(
            message_id=notice_id,
            conversation_id=message.conversation_id,
            sender=f"system:hyprial:{self.node_id}",
            recipient=message.sender,
            payload=payload,
            intent="system",
            lifecycle=DeliveryLifecycle.ONLINE_ONLY,
            idempotency_key=f"sender-rejected:{message.message_id}",
            created_at_ms=now_ms,
        )
        target_node = message.origin_node
        if target_node is None and self._transport.is_online(message.sender):
            target_node = message.sender
        if target_node is None or target_node == self.node_id:
            self.receive_system_notice(notice)
        else:
            self._transport.deliver_notice(target_node, notice)
        return ReceiveResult(
            message.message_id,
            accepted=False,
            acknowledged=False,
            code="SENDER_NOT_CANONICAL",
        )

    @property
    def alarm_emitter(self) -> AlarmEmitter:
        """The service's own emitter, shared with the PAC workflow service so
        escalations ride one throttle ledger and one delivery wiring."""
        return self._alarm

    @_synchronized
    def consumption_state(self, message_id: str) -> ConsumptionState:
        """One message's consumption state, for ack-kind awaits (PAC M2 first nail).

        Evidence order: the inbox row itself (consumed flag survives until
        prune), then the terminal ledger — a pruned-away consumption leaves a
        FETCHED record (``retire_outbox_receipt``), an eviction leaves EXPIRED.
        Absence of both is honestly UNKNOWN, never a guessed state.
        """

        row = self._db.execute(
            """SELECT inbox.consumed,
                      COALESCE(harness_failure_settlements.terminal, 0) AS terminal
                 FROM inbox
                 LEFT JOIN harness_failure_settlements
                   ON harness_failure_settlements.message_id = inbox.message_id
                WHERE inbox.message_id = ?""",
            (message_id,),
        ).fetchone()
        if row is not None:
            if bool(row["terminal"]):
                return ConsumptionState.FAILED
            return ConsumptionState.CONSUMED if row["consumed"] else ConsumptionState.PENDING
        record = self._status.lookup(message_id)
        if record is not None:
            if record.state is TerminalState.FETCHED:
                return ConsumptionState.CONSUMED
            if record.state is TerminalState.EXPIRED:
                return ConsumptionState.EXPIRED
        return ConsumptionState.UNKNOWN

    @_synchronized
    def pending_all(self) -> tuple[InboxMessage, ...]:
        """Every unconsumed inbox row, oldest first — the node-wide view.

        ``pending_messages(recipient)`` answers for one identity; ``ps``
        needs the whole durable inbox: post canonical-identity, traffic is
        agent-addressed, so a node-scoped read would report an empty node
        while rows wait under agent URIs.
        """

        rows = self._db.execute(
            """SELECT * FROM inbox
               WHERE consumed = 0
                 AND NOT EXISTS (
                     SELECT 1 FROM harness_failure_settlements
                      WHERE harness_failure_settlements.message_id = inbox.message_id
                        AND harness_failure_settlements.terminal = 1
                 )
               ORDER BY arrival_id"""
        ).fetchall()
        return tuple(self._row_message(row) for row in rows)

    @_synchronized
    def next(self, recipient: str) -> InboxMessage | None:
        row = self._db.execute(
            """SELECT * FROM inbox
               WHERE recipient = ? AND consumed = 0
                 AND NOT EXISTS (
                     SELECT 1 FROM harness_failure_settlements
                      WHERE harness_failure_settlements.message_id = inbox.message_id
                        AND harness_failure_settlements.terminal = 1
                 )
               ORDER BY arrival_id LIMIT 1""",
            (recipient,),
        ).fetchone()
        return self._row_message(row) if row is not None else None

    @_synchronized
    def ack(self, recipient: str, message_id: str) -> AckResult:
        now = self._now_ms()
        with self._db:
            row = self._db.execute(
                """SELECT * FROM inbox
                    WHERE recipient = ? AND message_id = ? AND consumed = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM harness_failure_settlements
                           WHERE harness_failure_settlements.message_id = inbox.message_id
                             AND harness_failure_settlements.terminal = 1
                      )""",
                (recipient, message_id),
            ).fetchone()
            if row is None:
                # That SELECT misses three states whose correct handling is
                # opposite, and returning one code for all of them makes a
                # successful idempotent retry read as "your ack did not work":
                #   * no such row            -> real failure, may be data loss
                #   * row present, settled   -> the caller's goal already holds
                #   * terminal settlement    -> closed by the failure path
                # Only the middle one is success. Ask for it specifically
                # rather than widening the query above, so the other two keep
                # failing exactly as before.
                settled = self._db.execute(
                    """SELECT 1 FROM inbox
                        WHERE recipient = ? AND message_id = ? AND consumed = 1
                          AND NOT EXISTS (
                              SELECT 1 FROM harness_failure_settlements
                               WHERE harness_failure_settlements.message_id = inbox.message_id
                                 AND harness_failure_settlements.terminal = 1
                          )""",
                    (recipient, message_id),
                ).fetchone()
                if settled is not None:
                    # Acking twice is a no-op that already achieved its goal.
                    # The code is carried alongside acknowledged=True so a
                    # caller can still tell "I settled it" from "it was
                    # already settled" -- they differ for auditing, not for
                    # control flow.
                    return AckResult(message_id, True, "MESSAGE_ALREADY_SETTLED")
                return AckResult(message_id, False, ipc_errors.MESSAGE_ACK_UNAVAILABLE)
            cursor = self._db.execute(
                """UPDATE inbox
                      SET consumed = 1,
                          acknowledged_at_ms = ?,
                          fetched_at_ms = COALESCE(fetched_at_ms, ?)
                   WHERE recipient = ? AND message_id = ? AND consumed = 0""",
                (now, now, recipient, message_id),
            )
        if cursor.rowcount != 1:
            return AckResult(message_id, False, ipc_errors.MESSAGE_ACK_UNAVAILABLE)
        message = self._row_message(row)
        self.retire_outbox_receipt(message.sender, message_id, now_ms=now)
        return AckResult(message_id, True)

    @_synchronized
    def refresh_hold(self, message_id: str, *, now_ms: int | None = None) -> bool:
        """Push an unconsumed row's TTL deadline out to ``now + durable_ttl_ms``.

        #276: ``expires_at_ms`` used to be stamped once at :meth:`receive`
        and never touched again, so a turn legitimately running longer than
        the hold TTL got its inbox row pruned mid-flight — the reply, once
        the turn finished, had nothing to ack against
        (``_complete_harness_results``' ``original is None`` path) and was
        silently lost. In-flight is not "nobody picked this up"; the caller
        (dispatch acceptance, then each worker progress event) calls this to
        say "still being worked", sliding the deadline like the PAC executor's
        ``_extend_on_activity`` does for the same reason. A worker that goes
        silent gets no more refreshes, so its row still expires
        ``durable_ttl_ms`` after the last refresh and ``prune_inbox`` still
        reaps it — this does not make the TTL unbounded, only activity-relative.
        A no-op (returns False) if the row is already consumed or gone.
        """
        now = self._now_ms() if now_ms is None else now_ms
        with self._db:
            cursor = self._db.execute(
                """UPDATE inbox SET expires_at_ms = ?
                    WHERE message_id = ? AND consumed = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM harness_failure_settlements
                           WHERE harness_failure_settlements.message_id = inbox.message_id
                             AND harness_failure_settlements.terminal = 1
                      )""",
                (now + self.durable_ttl_ms, message_id),
            )
        return cursor.rowcount > 0

    @_synchronized
    def fail(self, recipient: str, message_id: str, detail: str) -> FailureResult:
        now = self._now_ms()
        with self._db:
            row = self._db.execute(
                """SELECT * FROM inbox
                    WHERE recipient = ? AND message_id = ? AND consumed = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM harness_failure_settlements
                           WHERE harness_failure_settlements.message_id = inbox.message_id
                             AND harness_failure_settlements.terminal = 1
                      )""",
                (recipient, message_id),
            ).fetchone()
            if row is None:
                raise KeyError(message_id)
            self._db.execute(
                "INSERT INTO dlq(message_id, owner, payload, reason, failed_at_ms) VALUES (?, ?, ?, ?, ?)",
                (message_id, recipient, row["payload"], detail, now),
            )
            self._db.execute(
                "DELETE FROM inbox WHERE arrival_id = ?", (row["arrival_id"],)
            )
        self._emit_failure(self._row_message(row), detail)
        return FailureResult(message_id, "DELIVERY_FAILED_AFTER_ACCEPTANCE", message_id)

    @_synchronized
    def settle_harness_failure(
        self,
        recipient: str,
        message_id: str,
        failure_code: str,
        *,
        permanent: bool,
        max_attempts: int,
        backoff_ms: tuple[int, ...],
        now_ms: int | None = None,
    ) -> HarnessFailureSettlement:
        """Append failure evidence and, when due, mint a terminal tombstone.

        The inbox row is evidence and is never deleted by this transition.
        A terminal DLQ record is written in the same SQLite transaction, while
        every failed attempt remains independently auditable in the append-only
        attempt table.  Only stable codes are persisted; model-vendor prose can
        contain credentials and therefore never crosses this boundary.
        """

        if not failure_code or len(failure_code) > 200:
            raise ValueError("failure_code must be a bounded non-empty string")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if not backoff_ms or any(delay < 1 for delay in backoff_ms):
            raise ValueError("backoff_ms must contain positive delays")
        now = self._now_ms() if now_ms is None else now_ms
        with self._db:
            prior = self._db.execute(
                "SELECT * FROM harness_failure_settlements WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if prior is not None and bool(prior["terminal"]):
                return self._harness_failure_row(prior)
            inbox_row = self._db.execute(
                "SELECT * FROM inbox WHERE recipient = ? AND message_id = ? "
                "AND consumed = 0",
                (recipient, message_id),
            ).fetchone()
            if inbox_row is None:
                if prior is not None:
                    return self._harness_failure_row(prior)
                raise KeyError(message_id)
            attempts = 1 if prior is None else int(prior["attempts"]) + 1
            cycle = (
                int(prior["cycle"])
                if prior is not None
                else int(
                    self._db.execute(
                        """SELECT COALESCE(MAX(cycle), 0) + 1
                           FROM harness_failure_attempts WHERE message_id = ?""",
                        (message_id,),
                    ).fetchone()[0]
                )
            )
            effective_max_attempts = (
                max_attempts if prior is None else int(prior["max_attempts"])
            )
            terminal = permanent or attempts >= effective_max_attempts
            terminal_reason = (
                failure_code
                if permanent
                else f"{failure_code}_RETRY_EXHAUSTED" if terminal else None
            )
            next_attempt_ms = (
                None
                if terminal
                else now + backoff_ms[min(attempts - 1, len(backoff_ms) - 1)]
            )
            first_failed_at_ms = (
                now if prior is None else int(prior["first_failed_at_ms"])
            )
            self._db.execute(
                """INSERT INTO harness_failure_attempts (
                       message_id, cycle, attempt, failure_code, permanent,
                       failed_at_ms
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (message_id, cycle, attempts, failure_code, int(permanent), now),
            )
            self._db.execute(
                """INSERT INTO harness_failure_settlements (
                       message_id, recipient, cycle, failure_code, attempts,
                       max_attempts, next_attempt_ms, terminal, permanent, terminal_reason,
                       first_failed_at_ms, updated_at_ms, terminal_at_ms
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(message_id) DO UPDATE SET
                       recipient = excluded.recipient,
                       cycle = excluded.cycle,
                       failure_code = excluded.failure_code,
                       attempts = excluded.attempts,
                       max_attempts = excluded.max_attempts,
                       next_attempt_ms = excluded.next_attempt_ms,
                       terminal = excluded.terminal,
                       permanent = excluded.permanent,
                       terminal_reason = excluded.terminal_reason,
                       first_failed_at_ms = excluded.first_failed_at_ms,
                       updated_at_ms = excluded.updated_at_ms,
                       terminal_at_ms = excluded.terminal_at_ms""",
                (
                    message_id,
                    recipient,
                    cycle,
                    failure_code,
                    attempts,
                    effective_max_attempts,
                    next_attempt_ms,
                    int(terminal),
                    int(permanent),
                    terminal_reason,
                    first_failed_at_ms,
                    now,
                    now if terminal else None,
                ),
            )
            if terminal:
                assert terminal_reason is not None
                self._db.execute(
                    """INSERT INTO dlq(
                           message_id, owner, payload, reason, failed_at_ms
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        message_id,
                        recipient,
                        inbox_row["payload"],
                        terminal_reason,
                        now,
                    ),
                )
            else:
                assert next_attempt_ms is not None
                self._db.execute(
                    "UPDATE inbox SET expires_at_ms = MAX(expires_at_ms, ?) "
                    "WHERE recipient = ? AND message_id = ? AND consumed = 0",
                    (
                        next_attempt_ms + self.durable_ttl_ms,
                        recipient,
                        message_id,
                    ),
                )
            row = self._db.execute(
                "SELECT * FROM harness_failure_settlements WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        assert row is not None
        return self._harness_failure_row(row)

    @_synchronized
    def terminal_failure_settlements(
        self, *, since_ms: int
    ) -> tuple[HarnessFailureSettlement, ...]:
        """Terminal tombstones recorded at or after ``since_ms``.

        This is the durable half of the fail-loud promise: a settlement is the
        fact that a request never got a result, and it outlives the process
        that observed it.  A restarted daemon reads it to re-derive a sender
        notice it may not have managed to submit (see
        ``DaemonEventBridge._recover_owed_notices``), so the ``window`` is the
        previous run, not all history -- replaying years of old failures would
        be a new kind of wrong.
        """

        rows = self._db.execute(
            """SELECT * FROM harness_failure_settlements
               WHERE terminal = 1 AND updated_at_ms >= ?
               ORDER BY updated_at_ms""",
            (since_ms,),
        ).fetchall()
        return tuple(self._harness_failure_row(row) for row in rows)

    @_synchronized
    def harness_failure_settlement(
        self, message_id: str
    ) -> HarnessFailureSettlement | None:
        row = self._db.execute(
            "SELECT * FROM harness_failure_settlements WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return None if row is None else self._harness_failure_row(row)

    @_synchronized
    def harness_failure_original(self, message_id: str) -> InboxMessage | None:
        """The original request row, even when it is consumed or terminal.

        The fail-loud sender notice needs the route (``sender``,
        ``conversation_id``) of a request that failed *after* its row stopped
        being pending -- fetched by a pull consumer, acked, or already settled
        as terminal.  ``pending_messages`` deliberately hides those rows, so
        the notice would have no route without this read.  The row itself is
        retained either way; only the visibility window moved.
        """

        row = self._db.execute(
            "SELECT * FROM inbox WHERE message_id = ? LIMIT 1", (message_id,)
        ).fetchone()
        return None if row is None else self._row_message(row)

    @_synchronized
    def harness_failure_attempts(
        self, message_id: str
    ) -> tuple[HarnessFailureAttempt, ...]:
        rows = self._db.execute(
            """SELECT * FROM harness_failure_attempts
               WHERE message_id = ? ORDER BY cycle, attempt""",
            (message_id,),
        ).fetchall()
        return tuple(
            HarnessFailureAttempt(
                message_id=str(row["message_id"]),
                cycle=int(row["cycle"]),
                attempt=int(row["attempt"]),
                failure_code=str(row["failure_code"]),
                permanent=bool(row["permanent"]),
                failed_at_ms=int(row["failed_at_ms"]),
            )
            for row in rows
        )

    @staticmethod
    def _harness_failure_row(row: sqlite3.Row) -> HarnessFailureSettlement:
        return HarnessFailureSettlement(
            message_id=str(row["message_id"]),
            recipient=str(row["recipient"]),
            cycle=int(row["cycle"]),
            failure_code=str(row["failure_code"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            next_attempt_ms=(
                None
                if row["next_attempt_ms"] is None
                else int(row["next_attempt_ms"])
            ),
            terminal=bool(row["terminal"]),
            permanent=bool(row["permanent"]),
            terminal_reason=(
                None
                if row["terminal_reason"] is None
                else str(row["terminal_reason"])
            ),
            first_failed_at_ms=int(row["first_failed_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
            terminal_at_ms=(
                None if row["terminal_at_ms"] is None else int(row["terminal_at_ms"])
            ),
        )

    @_synchronized
    def accept_custody(
        self, message: InboxMessage, *, mailbox_node: str, now_ms: int | None = None
    ) -> AckResult:
        now = self._now_ms() if now_ms is None else now_ms
        used = int(
            self._db.execute(
                "SELECT COALESCE(SUM(length(payload)), 0) FROM custody"
            ).fetchone()[0]
        )
        existing = self._db.execute(
            "SELECT 1 FROM custody WHERE message_id = ?", (message.message_id,)
        ).fetchone()
        if existing is not None:
            return AckResult(message.message_id, True)
        if used + len(message.payload) > self.max_custody_bytes:
            return AckResult(message.message_id, False, "MAILBOX_CAPACITY_EXCEEDED")
        with self._db:
            self._db.execute(
                """INSERT INTO custody (
                       message_id, mailbox_node, conversation_id, sender, recipient,
                       payload, intent, lifecycle, idempotency_key, created_at_ms,
                       accepted_at_ms, expires_at_ms, next_attempt_ms
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message.message_id,
                    mailbox_node,
                    message.conversation_id,
                    message.sender,
                    message.recipient,
                    message.payload,
                    message.intent,
                    message.lifecycle.value,
                    message.idempotency_key,
                    message.created_at_ms,
                    # ``accepted_at_ms`` and the hold deadline both come off
                    # this mailbox's clock, at the instant custody transfers.
                    now,
                    now + self.custody_ttl_ms,
                    now,
                ),
            )
        self._enforce_hold_capacity("custody", now)
        return AckResult(message.message_id, True)

    @_synchronized
    def retry_custody_due(self, *, now_ms: int | None = None) -> list[SubmissionResult]:
        now = self._now_ms() if now_ms is None else now_ms
        rows = self._db.execute(
            "SELECT * FROM custody WHERE next_attempt_ms <= ? ORDER BY accepted_at_ms, rowid",
            (now,),
        ).fetchall()
        outcomes: list[SubmissionResult] = []
        for row in rows:
            message = self._row_message(row)
            if now >= int(row["expires_at_ms"]):
                # Expiry is measured against this mailbox's own clock, from
                # ``accepted_at_ms`` -- the instant it took the message.  The
                # sender's ``created_at_ms`` never enters the arithmetic: two
                # machines' clocks routinely differ, and an age computed from a
                # foreign clock comes out negative or expires early.
                self._custody_to_dlq(row, "CUSTODY_EXPIRED", now)
                outcomes.append(
                    SubmissionResult(message.message_id, False, code="CUSTODY_EXPIRED")
                )
                continue
            if self._transport.is_online(message.recipient) and self._transport.deliver(
                message
            ):
                # Same correction as ``_attempt_direct``: ``now`` predates the
                # blocking delivery, so the record would claim to have been
                # written before the event it records.
                confirmed_at_ms = self._now_ms()
                with self._db:
                    self._record_terminal(
                        message,
                        state=TerminalState.FETCHED,
                        reason=HoldReason.RECEIPT_CONFIRMED,
                        now_ms=confirmed_at_ms,
                        holder=str(row["mailbox_node"]),
                    )
                    self._db.execute(
                        "DELETE FROM custody WHERE message_id = ?",
                        (message.message_id,),
                    )
                outcomes.append(
                    SubmissionResult(message.message_id, True, queued=False)
                )
                continue
            attempts = int(row["attempts"]) + 1
            schedule = self._retry_schedule(message.recipient)
            delay = schedule[min(attempts - 1, len(schedule) - 1)]
            with self._db:
                self._db.execute(
                    """UPDATE custody
                          SET attempts = ?,
                              next_attempt_ms = MIN(expires_at_ms, ?)
                        WHERE message_id = ?""",
                    (attempts, now + delay * 1000, message.message_id),
                )
            outcomes.append(SubmissionResult(message.message_id, True, queued=True))
        return outcomes

    @_synchronized
    def outbox_item(self, message_id: str) -> OutboxItem:
        row = self._db.execute(
            "SELECT * FROM outbox WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise KeyError(message_id)
        return OutboxItem(
            self._row_message(row),
            attempts=int(row["attempts"]),
            next_attempt_ms=int(row["next_attempt_ms"]),
            expires_at_ms=int(row["expires_at_ms"]),
        )

    def _row_message(self, row: sqlite3.Row) -> InboxMessage:
        return InboxMessage(
            message_id=str(row["message_id"]),
            conversation_id=str(row["conversation_id"]),
            sender=str(row["sender"]),
            recipient=str(row["recipient"]),
            payload=bytes(row["payload"]),
            intent=str(row["intent"]),
            lifecycle=DeliveryLifecycle(str(row["lifecycle"])),
            idempotency_key=row["idempotency_key"],
            created_at_ms=int(row["created_at_ms"]),
            # Only the inbox table carries the column; outbox/custody/
            # system_notices rows flow through this same helper.
            origin_node=(
                str(row["origin_node"])
                if "origin_node" in row.keys() and row["origin_node"] is not None
                else None
            ),
        )

    def _record_terminal(
        self,
        message: InboxMessage,
        *,
        state: TerminalState,
        reason: str,
        now_ms: int,
        holder: str | None = None,
    ) -> None:
        """Persist one terminal outcome.  Runs inside the caller's transaction.

        Never opens a transaction of its own: the record has to commit with the
        state change it describes, or a crash in between leaves a message gone
        with nothing saying where it went -- the silent drop this whole step
        exists to abolish.
        """

        self._status.record(
            message_id=message.message_id,
            sender=message.sender,
            recipient=message.recipient,
            state=state,
            holder=holder if holder is not None else self.node_id,
            reason=str(reason),
            now_ms=now_ms,
            conversation_id=message.conversation_id,
            idempotency_key=message.idempotency_key,
        )

    def _custody_to_dlq(self, row: sqlite3.Row, reason: str, now_ms: int) -> None:
        """Mailbox-side twin of ``_outbox_to_dlq``: evict, but observably."""

        message = self._row_message(row)
        with self._db:
            self._record_terminal(
                message,
                state=TerminalState.EXPIRED,
                reason=reason,
                now_ms=now_ms,
                holder=str(row["mailbox_node"]),
            )
            self._db.execute(
                "INSERT INTO dlq(message_id, owner, payload, reason, failed_at_ms) VALUES (?, ?, ?, ?, ?)",
                (
                    message.message_id,
                    row["mailbox_node"],
                    message.payload,
                    reason,
                    now_ms,
                ),
            )
            self._db.execute(
                "DELETE FROM custody WHERE message_id = ?", (message.message_id,)
            )
        self._emit_failure(message, reason)

    def _enforce_hold_capacity(self, table: str, now_ms: int) -> None:
        """Cap how many unfetched messages one holder keeps (section 6).

        Overflow is decided by ``rowid`` -- this node's own insertion order --
        for the same reason FIFO is ordered by ``arrival_id`` and never by send
        time (section 8.3): a sender's timestamp is a foreign clock and cannot
        order anything here.  Evicted messages go out the observable way, with
        an ``expired`` record, exactly like a TTL eviction.
        """

        query = {
            "outbox": "SELECT * FROM outbox ORDER BY rowid DESC LIMIT -1 OFFSET ?",
            "custody": "SELECT * FROM custody ORDER BY rowid DESC LIMIT -1 OFFSET ?",
        }[table]
        overflow = self._db.execute(query, (self.hold_policy.max_items,)).fetchall()
        for row in overflow:
            reason = HoldReason.HOLD_CAPACITY_EXCEEDED.value
            if table == "custody":
                self._custody_to_dlq(row, reason, now_ms)
            else:
                self._outbox_to_dlq(row, reason, now_ms)

    def _delete_outbox(self, message_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM outbox WHERE message_id = ?", (message_id,))

    @_synchronized
    def retire_outbox_receipt(
        self,
        sender: str,
        message_id: str,
        *,
        now_ms: int | None = None,
    ) -> bool:
        """Retire one sender-held row after a recipient fetch receipt.

        The sender identity is part of the predicate, matching the existing
        receipt key.  This makes repeated or mesh-wide receipt publications
        idempotent and prevents an unrelated receipt with the same message ID
        from deleting a row owned by another sender.
        """

        row = self._db.execute(
            "SELECT * FROM outbox WHERE message_id = ? AND sender = ?",
            (message_id, sender),
        ).fetchone()
        if row is None:
            return False
        message = self._row_message(row)
        confirmed_at_ms = self._now_ms() if now_ms is None else now_ms
        with self._db:
            self._record_terminal(
                message,
                state=TerminalState.FETCHED,
                reason=HoldReason.RECEIPT_CONFIRMED,
                now_ms=confirmed_at_ms,
            )
            self._db.execute(
                "DELETE FROM outbox WHERE message_id = ? AND sender = ?",
                (message_id, sender),
            )
        return True

    def _outbox_to_dlq(
        self,
        row: sqlite3.Row,
        reason: str,
        now_ms: int,
        *,
        emit_failure: bool = True,
        local_notice: InboxMessage | None = None,
    ) -> None:
        # Single choke point for "a held message leaves this holder without
        # having been fetched" -- TTL, retry exhaustion, an undeliverable
        # address, or the hold-capacity cap all pass through here.  Recording
        # the terminal state inside the same transaction is what turns every
        # one of those from a silent drop into an observable ``expired``.
        message = self._row_message(row)
        with self._db:
            self._record_terminal(
                message,
                state=TerminalState.EXPIRED,
                reason=reason,
                now_ms=now_ms,
            )
            self._db.execute(
                "INSERT INTO dlq(message_id, owner, payload, reason, failed_at_ms) VALUES (?, ?, ?, ?, ?)",
                (row["message_id"], row["sender"], row["payload"], reason, now_ms),
            )
            self._db.execute(
                "DELETE FROM outbox WHERE message_id = ?", (row["message_id"],)
            )
            if local_notice is not None:
                self._persist_system_notice_locked(local_notice)
        self._log_dlq(message, int(row["attempts"]), reason, now_ms)
        if emit_failure:
            self._emit_failure(message, reason)

    def _log_retry(
        self,
        message: InboxMessage,
        attempts: int,
        next_attempt_ms: int,
        reason: str,
    ) -> None:
        """Every backoff step is a log event, not just a DB column.

        The retry/DLQ path used to be write-only to SQLite: a message dying in
        the outbox was invisible in logs until ``harness.delivery.*`` fired
        (if it ever did), which is how reports retried into the DLQ unnoticed
        (2026-09-14 observability gap). Identifiers and counters only.
        """

        try:
            self._logger.log(
                "warn",
                "outbox.retry_scheduled",
                messageId=message.message_id,
                recipient=message.recipient,
                sender=message.sender,
                attempts=attempts,
                nextAttemptMs=next_attempt_ms,
                reason=reason,
            )
        except Exception:  # noqa: BLE001 - logging must never break delivery
            pass

    def _log_dlq(
        self, message: InboxMessage, attempts: int, reason: str, now_ms: int
    ) -> None:
        """The terminal leg: a message that will never be retried again."""

        try:
            self._logger.log(
                "error",
                "outbox.terminal_failed",
                messageId=message.message_id,
                recipient=message.recipient,
                sender=message.sender,
                attempts=attempts,
                reason=reason,
                failedAtMs=now_ms,
            )
        except Exception:  # noqa: BLE001 - logging must never break delivery
            pass

    def _emit_failure(self, message: InboxMessage, reason: str) -> None:
        self._alarm.emit(
            Alarm(
                correlation_id=message.message_id,
                message_id=message.message_id,
                conversation_id=message.conversation_id,
                sender=message.sender,
                recipient=message.recipient,
                reason=str(reason),
                audience=audience_for_sender(message.sender),
            )
        )

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
        from hyprial.uri import parse_agent_uri

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
            )
            for row in rows
        )

    @_synchronized
    def prune_outbox(
        self,
        *,
        undeliverable: Callable[[str], bool],
        unresolvable: Callable[[str], bool] | None = None,
        now_ms: int | None = None,
        dry_run: bool = False,
    ) -> tuple[OutboxPruneItem, ...]:
        """Move dead outbox entries to the DLQ; leave in-flight entries alone.

        Dead means provably undeliverable: the recipient fails the caller's
        address predicate (a scheme this build can never deliver), or the
        durable TTL has expired, or — C1 (2026-08-22 dead-letter audit) — the
        optional `unresolvable` predicate proves the target does not exist.
        The caller owns the unresolvable semantics: it must distinguish
        TEMPORARILY OFFLINE (never prunable: the worker may come back) from
        PROVABLY NONEXISTENT (prunable).  Retry pressure alone is not death —
        an offline-but-deliverable recipient keeps its entry.
        """

        now = self._now_ms() if now_ms is None else now_ms
        rows = self._db.execute(
            "SELECT * FROM outbox ORDER BY created_at_ms, rowid"
        ).fetchall()
        pruned: list[OutboxPruneItem] = []
        for row in rows:
            recipient = str(row["recipient"])
            if undeliverable(recipient):
                reason = "UNDELIVERABLE_SCHEME"
            elif unresolvable is not None and unresolvable(recipient):
                reason = "TARGET_UNRESOLVABLE"
            elif now >= int(row["expires_at_ms"]):
                reason = "DELIVERY_EXPIRED"
            else:
                continue
            if not dry_run:
                self._outbox_to_dlq(row, reason, now)
            pruned.append(
                OutboxPruneItem(
                    message_id=str(row["message_id"]),
                    recipient=recipient,
                    reason=reason,
                    created_at_ms=int(row["created_at_ms"]),
                    attempts=int(row["attempts"]),
                )
            )
        return tuple(pruned)

    @_synchronized
    def prune_inbox(
        self, *, now_ms: int | None = None
    ) -> tuple[InboxPruneItem, ...]:
        """Evict unconsumed inbox rows past their TTL deadline.

        The recipient node committed these messages (a ``fetched`` terminal
        record exists), but no actor ever consumed them: a recipient URI
        mismatch after a node rename, a decommissioned actor, or a consumer
        that stopped polling.  Left alone they accumulate forever.  Expiry
        is measured against this node's own clock from ``received_at_ms`` --
        never from the sender's foreign ``created_at_ms`` (section 6).

        A row still retrying a failed harness delivery is kept, but the
        mechanism is the deadline itself, not a sweep-side exemption:
        ``settle_harness_failure`` pushes ``expires_at_ms`` out to
        ``next_attempt_ms + durable_ttl_ms`` on every non-terminal failure,
        so a row mid-backoff simply is not expired yet.  A *terminal*
        settlement is the opposite case -- the delivery is tombstoned and
        every consumer path already ignores the row, so once the deadline
        passes the row is evicted like any other expired row, reported with
        reason ``TERMINAL_SETTLED`` instead of ``INBOX_TTL_EXPIRED``.  The
        settlement row itself is never deleted here: it is the durable
        tombstone that keeps answering for the failure.  (The old
        ``NOT EXISTS terminal = 1`` clause had this exactly backwards -- it
        exempted the one population that was already dead, which is how
        consumed=0 zombie rows accumulated that neither dispatched nor
        pruned.)

        Acknowledged rows are deliberately NOT swept: the retention contract
        keeps consumed rows until their message id is reused, so they only
        disappear when the same id arrives again.  A row with a NULL
        ``expires_at_ms`` (a pre-backfill legacy row that the migration
        missed) is also left alone rather than guessed at.
        """

        now = self._now_ms() if now_ms is None else now_ms
        rows = self._db.execute(
            """SELECT inbox.*,
                      COALESCE(harness_failure_settlements.terminal, 0)
                          AS settled_terminal
                 FROM inbox
                 LEFT JOIN harness_failure_settlements
                   ON harness_failure_settlements.message_id = inbox.message_id
                WHERE inbox.consumed = 0
                  AND inbox.expires_at_ms IS NOT NULL
                  AND inbox.expires_at_ms <= ?
                ORDER BY inbox.arrival_id""",
            (now,),
        ).fetchall()
        if not rows:
            return ()
        with self._db:
            self._db.execute(
                """DELETE FROM inbox
                   WHERE consumed = 0
                     AND expires_at_ms IS NOT NULL
                     AND expires_at_ms <= ?""",
                (now,),
            )
        return tuple(
            InboxPruneItem(
                message_id=str(row["message_id"]),
                recipient=str(row["recipient"]),
                reason=(
                    "TERMINAL_SETTLED"
                    if row["settled_terminal"]
                    else "INBOX_TTL_EXPIRED"
                ),
                created_at_ms=int(row["created_at_ms"]),
                received_at_ms=int(row["received_at_ms"]),
            )
            for row in rows
        )

    @_synchronized
    def pending_count(self, recipient: str) -> int:
        return int(
            self._db.execute(
                """SELECT COUNT(*) FROM inbox
                   WHERE recipient = ? AND consumed = 0
                     AND NOT EXISTS (
                         SELECT 1 FROM harness_failure_settlements
                          WHERE harness_failure_settlements.message_id = inbox.message_id
                            AND harness_failure_settlements.terminal = 1
                     )""",
                (recipient,),
            ).fetchone()[0]
        )

    @_synchronized
    def pending_recipient_counts(self) -> tuple[tuple[str, int], ...]:
        """Summarize every durable consumer key without exposing payloads."""

        rows = self._db.execute(
            """SELECT recipient, COUNT(*) AS pending
               FROM inbox
               WHERE consumed = 0
                 AND NOT EXISTS (
                     SELECT 1 FROM harness_failure_settlements
                      WHERE harness_failure_settlements.message_id = inbox.message_id
                        AND harness_failure_settlements.terminal = 1
                 )
               GROUP BY recipient
               ORDER BY recipient"""
        ).fetchall()
        return tuple((str(row["recipient"]), int(row["pending"])) for row in rows)

    @_synchronized
    def unfetched_recipient_stats(self) -> tuple[tuple[str, int, int], ...]:
        """Per-recipient depth and oldest arrival over mail NOBODY HAS TAKEN.

        "Taken" has TWO spellings, because the two dispatch paths mark
        responsibility differently and a check that knows only one reports
        the other as broken:

        * the pull path (``harness_read`` -> ``fetch_pending``) stamps
          ``fetched_at_ms``;
        * the streaming path never touches that column -- it calls
          ``refresh_hold`` when the worker ACCEPTS the delivery into its
          queue (#276), which pushes ``expires_at_ms`` past
          ``received_at_ms + hold_ttl_ms``.  That gap is the only trace it
          leaves, and it is what tells a queued delivery apart from one
          nobody has looked at.

        Reading ``fetched_at_ms IS NULL`` alone would therefore report every
        healthy streaming worker with a queued message as "not collecting".
        """

        rows = self._db.execute(
            """SELECT recipient, COUNT(*) AS pending, MIN(received_at_ms) AS oldest
               FROM inbox
               WHERE consumed = 0
                 AND fetched_at_ms IS NULL
                 AND expires_at_ms <= received_at_ms + ?
                 AND NOT EXISTS (
                     SELECT 1 FROM harness_failure_settlements
                      WHERE harness_failure_settlements.message_id = inbox.message_id
                        AND harness_failure_settlements.terminal = 1
                 )
               GROUP BY recipient
               ORDER BY recipient""",
            (self.durable_ttl_ms,),
        ).fetchall()
        return tuple(
            (str(row["recipient"]), int(row["pending"]), int(row["oldest"]))
            for row in rows
        )

    @_synchronized
    def pending_recipient_stats(self) -> tuple[tuple[str, int, int], ...]:
        """Per-recipient pending depth and oldest arrival, without payloads."""

        rows = self._db.execute(
            """SELECT recipient, COUNT(*) AS pending, MIN(received_at_ms) AS oldest
               FROM inbox
               WHERE consumed = 0
                 AND NOT EXISTS (
                     SELECT 1 FROM harness_failure_settlements
                      WHERE harness_failure_settlements.message_id = inbox.message_id
                        AND harness_failure_settlements.terminal = 1
                 )
               GROUP BY recipient
               ORDER BY recipient"""
        ).fetchall()
        return tuple(
            (str(row["recipient"]), int(row["pending"]), int(row["oldest"]))
            for row in rows
        )

    @_synchronized
    def pending_messages(self, recipient: str) -> tuple[InboxMessage, ...]:
        rows = self._db.execute(
            """SELECT * FROM inbox
               WHERE recipient = ? AND consumed = 0
                 AND NOT EXISTS (
                     SELECT 1 FROM harness_failure_settlements
                      WHERE harness_failure_settlements.message_id = inbox.message_id
                        AND harness_failure_settlements.terminal = 1
                 )
               ORDER BY arrival_id""",
            (recipient,),
        ).fetchall()
        return tuple(self._row_message(row) for row in rows)

    @_synchronized
    def dispatchable_messages(
        self, recipient: str, *, now_ms: int | None = None
    ) -> tuple[InboxMessage, ...]:
        """Pending rows whose durable failure backoff has elapsed."""

        now = self._now_ms() if now_ms is None else now_ms
        rows = self._db.execute(
            """SELECT * FROM inbox
               WHERE recipient = ? AND consumed = 0
                 AND NOT EXISTS (
                     SELECT 1 FROM harness_failure_settlements
                      WHERE harness_failure_settlements.message_id = inbox.message_id
                        AND (
                            harness_failure_settlements.terminal = 1
                            OR harness_failure_settlements.next_attempt_ms > ?
                        )
                 )
               ORDER BY arrival_id""",
            (recipient, now),
        ).fetchall()
        return tuple(self._row_message(row) for row in rows)

    @_synchronized
    def fetch_pending(
        self, recipient: str, *, now_ms: int | None = None
    ) -> tuple[InboxMessage, ...]:
        """Record one real pull and retire same-daemon sender holds.

        Background Channel observation must use ``pending_messages``.  This
        method is reserved for the public ``harness_read`` fetch boundary.
        """

        now = self._now_ms() if now_ms is None else now_ms
        rows = self._db.execute(
            """SELECT * FROM inbox
               WHERE recipient = ? AND consumed = 0
                 AND NOT EXISTS (
                     SELECT 1 FROM harness_failure_settlements
                      WHERE harness_failure_settlements.message_id = inbox.message_id
                        AND harness_failure_settlements.terminal = 1
                 )
               ORDER BY arrival_id""",
            (recipient,),
        ).fetchall()
        messages = tuple(self._row_message(row) for row in rows)
        with self._db:
            self._db.executemany(
                """UPDATE inbox SET fetched_at_ms = COALESCE(fetched_at_ms, ?)
                    WHERE message_id = ?""",
                ((now, message.message_id) for message in messages),
            )
        for message in messages:
            self.retire_outbox_receipt(message.sender, message.message_id, now_ms=now)
        return messages

    @_synchronized
    def has_fetched(self, message_id: str) -> bool:
        row = self._db.execute(
            "SELECT fetched_at_ms FROM inbox WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None and row["fetched_at_ms"] is not None

    @_synchronized
    def dlq_count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM dlq").fetchone()[0])

    @_synchronized
    def custody_count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM custody").fetchone()[0])

    @_synchronized
    def is_acknowledged(self, message_id: str) -> bool:
        row = self._db.execute(
            "SELECT consumed FROM inbox WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None and bool(row["consumed"])

    @_synchronized
    def has_received(self, message_id: str) -> bool:
        row = self._db.execute(
            """SELECT 1 FROM inbox WHERE message_id = ?
               UNION ALL
               SELECT 1 FROM dedup WHERE dedup_key = ?
               LIMIT 1""",
            (message_id, f"msgid:{message_id}"),
        ).fetchone()
        return row is not None

    @property
    def delivery_status(self) -> DeliveryStatusStore:
        """The terminal-record ledger, for the node's status queryable."""

        return self._status

    @_synchronized
    def delivery_status_records(
        self, sender: str, *, message_id: str | None = None
    ) -> tuple[DeliveryStatus, ...]:
        """Terminal records this node holds for messages ``sender`` sent."""

        return self._status.for_sender(sender, message_id=message_id)

    @_synchronized
    def held_expiry_ms(self, message_id: str) -> int | None:
        """When this holder will evict the message, or None if it holds none.

        A held message has no terminal state yet; this is what distinguishes
        "still in flight here" from "this node never had it".
        """

        row = self._db.execute(
            """SELECT expires_at_ms FROM outbox WHERE message_id = ?
               UNION ALL
               SELECT expires_at_ms FROM custody WHERE message_id = ?
               LIMIT 1""",
            (message_id, message_id),
        ).fetchone()
        return None if row is None else int(row["expires_at_ms"])

    @_synchronized
    def has_custody(self, message_id: str) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM custody WHERE message_id = ?", (message_id,)
            ).fetchone()
            is not None
        )

    @_synchronized
    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
