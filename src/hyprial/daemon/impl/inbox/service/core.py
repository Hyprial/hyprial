from __future__ import annotations
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from hyprial.daemon import AlarmDelivery, AlarmEmitter
from hyprial.kernel import Logger
from hyprial.daemon.impl.inbox.contracts.api  import (
    DeliveryLifecycle,
    DeliveryTransport,
    InboxMessage,
    SubmissionResult,
)
from hyprial.daemon.impl.inbox.links.pull  import (
    DEFAULT_HOLD_TTL_MS,
    DeliveryStatusStore,
    HoldPolicy,
    HoldReason,
    TerminalState,
)
from hyprial.daemon.impl.inbox.service.state import ConsumptionState
from hyprial.daemon.impl.inbox.service.policy import (
    DELIVERY_RETRY_CLAIM_LIMIT,
    RetryPolicy,
    online_retry_next_attempt_ms,
)
from hyprial.daemon.impl.inbox.service.synchronization import (
    _ActorMutationContext,
    _synchronized,
)

"""SQLite-backed outbox, inbox, deduplication, FIFO, DLQ and custody state."""
DAY_MS = 86_400_000

class InboxServiceCoreMixin:
    dedup_window_ms = DAY_MS
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
        # TODO(PR 3): migrate old outbox rows atomically, then drop the legacy
        # retry_started_at_ms / confirmation_* columns. Keep this note out of
        # the SQL: SQLite stores the CREATE text verbatim, and a comment inside
        # the column list breaks ALTER TABLE ... DROP COLUMN ("incomplete input").
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
                message_expires_at_ms INTEGER,
                expires_at_ms INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_ms INTEGER NOT NULL,
                last_error TEXT,
                retry_started_at_ms INTEGER,
                confirmation_started_at_ms INTEGER,
                confirmation_attempts INTEGER NOT NULL DEFAULT 0,
                confirmation_reason TEXT
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
                message_expires_at_ms INTEGER,
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
                message_expires_at_ms INTEGER,
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
        outbox_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(outbox)").fetchall()
        }
        if "retry_started_at_ms" not in outbox_columns:
            self._db.execute("ALTER TABLE outbox ADD COLUMN retry_started_at_ms INTEGER")
        if "confirmation_started_at_ms" not in outbox_columns:
            self._db.execute(
                "ALTER TABLE outbox ADD COLUMN confirmation_started_at_ms INTEGER"
            )
        if "confirmation_attempts" not in outbox_columns:
            self._db.execute(
                "ALTER TABLE outbox ADD COLUMN confirmation_attempts "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "confirmation_reason" not in outbox_columns:
            self._db.execute("ALTER TABLE outbox ADD COLUMN confirmation_reason TEXT")
        self._db.execute(
            "UPDATE outbox SET retry_started_at_ms = ? "
            "WHERE retry_started_at_ms IS NULL",
            (self._now_ms(),),
        )
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
        for table in ("outbox", "inbox", "custody"):
            columns = {
                str(row["name"])
                for row in self._db.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if "message_expires_at_ms" not in columns:
                # Existing effective expiries were holder-local TTLs.  They
                # must remain NULL here rather than becoming producer
                # deadlines that a later hop would interpret as absolute.
                self._db.execute(
                    f"ALTER TABLE {table} ADD COLUMN message_expires_at_ms INTEGER"
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
            and not self._recipient_online(message)
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
            return SubmissionResult(message.message_id, accepted=True, queued=True)
        if self._recipient_online(message) and self._attempt_direct(
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
        if not self._recipient_online(message):
            self._defer_outbox(message, now)
        return SubmissionResult(message.message_id, accepted=True, queued=True)
    def _recipient_online(self, message: InboxMessage) -> bool:
        if message.lifecycle == DeliveryLifecycle.ONLINE_ONLY:
            live = getattr(self._transport, "is_online_only_live", None)
            if callable(live):
                return bool(live(message.recipient))
        return self._transport.is_online(message.recipient)
    def _insert_outbox(self, message: InboxMessage, now_ms: int) -> None:
        self._db.execute(
            """INSERT OR IGNORE INTO outbox (
                   message_id, conversation_id, sender, recipient, payload, intent,
                   lifecycle, idempotency_key, created_at_ms,
                   message_expires_at_ms, expires_at_ms, next_attempt_ms,
                   retry_started_at_ms
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                message.expires_at_ms,
                # Ordinary messages use this holder's own clock plus the TTL.
                # A producer deadline is an explicit absolute work-order
                # boundary, never inferred from foreign ``created_at_ms``.
                (
                    message.expires_at_ms
                    if message.expires_at_ms is not None
                    else now_ms + self.durable_ttl_ms
                ),
                now_ms,
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
                    reason=HoldReason.ACK_RECEIVED,
                    now_ms=confirmed_at_ms,
                )
                self._db.execute(
                    "DELETE FROM outbox WHERE message_id = ?", (message.message_id,)
                )
            return True
        row = self._db.execute(
            "SELECT attempts, expires_at_ms FROM outbox WHERE message_id = ?",
            (message.message_id,),
        ).fetchone()
        if row is not None:
            attempts = int(row["attempts"]) + 1
            next_attempt_ms = online_retry_next_attempt_ms(
                message_id=message.message_id,
                attempts=attempts,
                now_ms=now_ms,
                expires_at_ms=int(row["expires_at_ms"]),
            )
            self._db.execute(
                """UPDATE outbox
                      SET attempts = ?,
                          next_attempt_ms = MIN(expires_at_ms, ?),
                          last_error = ?
                    WHERE message_id = ?""",
                (
                    attempts,
                    next_attempt_ms,
                    "ack not received",
                    message.message_id,
                ),
            )
            self._db.commit()
            self._log_retry(
                message,
                attempts,
                next_attempt_ms,
                "ack not received",
            )
        return False
    def _attempt_custody(self, message: InboxMessage) -> str | None:
        for mailbox in self._transport.online_mailboxes():
            if self._transport.transfer_custody(mailbox, message):
                return mailbox
        return None
    def _defer_outbox(self, message: InboxMessage, now_ms: int) -> None:
        """Park an offline row at its TTL; only a liveliness event advances it."""

        del now_ms
        with self._db:
            self._db.execute(
                """UPDATE outbox
                      SET next_attempt_ms = expires_at_ms,
                          last_error = ?
                    WHERE message_id = ?""",
                ("recipient offline", message.message_id),
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
            "SELECT * FROM outbox WHERE next_attempt_ms <= ? "
            "ORDER BY created_at_ms, rowid LIMIT ?",
            (now, DELIVERY_RETRY_CLAIM_LIMIT),
        ).fetchall()
        outcomes: list[SubmissionResult] = []
        for row in rows:
            message = self._row_message(row)
            expired = now >= int(row["expires_at_ms"])
            if expired:
                self._outbox_to_dlq(row, "TTL_EXPIRED", now)
                outcomes.append(
                    SubmissionResult(message.message_id, False, code="TTL_EXPIRED")
                )
            else:
                recipient_online = self._transport.is_online(message.recipient)
                if not recipient_online:
                    self._defer_outbox(message, now)
                    outcomes.append(
                        SubmissionResult(message.message_id, True, queued=True)
                    )
                elif self._attempt_direct(message, now):
                    outcomes.append(
                        SubmissionResult(message.message_id, True, queued=False)
                    )
                else:
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
                        outcomes.append(
                            SubmissionResult(message.message_id, True, queued=True)
                        )
        outcomes.extend(self.retry_custody_due(now_ms=now))
        return outcomes
    @_synchronized
    def wake_outbox_recipient(
        self, recipient: str, *, now_ms: int | None = None
    ) -> bool:
        """Make one recipient's live delayed rows eligible for the next pump."""

        now = self._now_ms() if now_ms is None else now_ms
        with self._db:
            outbox = self._db.execute(
                """UPDATE outbox
                      SET next_attempt_ms = ?
                    WHERE recipient = ?
                      AND expires_at_ms > ?
                      AND next_attempt_ms > ?""",
                (now, recipient, now, now),
            )
            custody = self._db.execute(
                """UPDATE custody
                      SET next_attempt_ms = ?
                    WHERE recipient = ?
                      AND expires_at_ms > ?
                      AND next_attempt_ms > ?""",
                (now, recipient, now, now),
            )
        return outbox.rowcount > 0 or custody.rowcount > 0
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
