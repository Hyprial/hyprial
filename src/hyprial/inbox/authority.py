"""Full inbox mutation authority facade and read-only projection seam."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from hyprial.alarm import Alarm, AlarmDelivery, AlarmResult

from .actor import DeliveryCustodyCoordinator, InboxAuthorityTimeout
from .api import (
    AckResult,
    DeliveryLifecycle,
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
from .ports import (
    AcceptCustodyCommand,
    AlarmCompleted,
    AcknowledgeCompleted,
    AcknowledgeMessageCommand,
    BoolMutationCompleted,
    CloseInboxCommand,
    DismissSystemNoticeCommand,
    DrainSystemNoticesCommand,
    EmitAlarmCommand,
    FailMessageCommand,
    FailureMutationCompleted,
    HarnessFailureSettled,
    FetchPendingCommand,
    InboxClosed,
    InboxPruneCompleted,
    MessagesMutationCompleted,
    OutboxPruneCompleted,
    PruneInboxCommand,
    PruneOutboxCommand,
    ReceiveCompleted,
    ReceiveMessageCommand,
    ReceiveProgressCommand,
    ReceiveSystemNoticeCommand,
    RefreshHoldCommand,
    RetireOutboxReceiptCommand,
    RetryCustodyCommand,
    RetryDueCommand,
    RetryIoCompleted,
    SettleHarnessFailureCommand,
    SubmissionBatchCompleted,
    SubmissionCompleted,
    SubmissionProjection,
    SubmitMessageCommand,
    SubmitProgressCommand,
)
from .progress import ProgressEvent
from .pull import DEFAULT_HOLD_TTL_MS, DeliveryStatus, TerminalState
from .service import ConsumptionState

_REPLY_COMPLETION_HANDOFF_GRACE_SECONDS = 0.5

#: The synchronous settle bound every ``DeliveryCustodyFacade`` mutation
#: waits inside (``coordinator.call`` timeout). Production constructs the
#: facade exactly once (``daemon/application.py``) and never overrides it,
#: so this default IS the budget the product runs with; naming it at module
#: level only moves an existing declaration to somewhere importable — it
#: does not choose a new number. Tests derive wait budgets from this path
#: (``tests/waiting_support.py``) instead of hand-copying the literal.
DELIVERY_CUSTODY_CALL_TIMEOUT_SECONDS = 2.0


class InboxReadProjection:
    """Stable read-only SQLite face; every connection is opened ``mode=ro``."""

    def __init__(self, database: Path) -> None:
        self._database = database

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            f"{self._database.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=0.1,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _message(row: sqlite3.Row) -> InboxMessage:
        return InboxMessage(
            message_id=str(row["message_id"]),
            conversation_id=str(row["conversation_id"]),
            sender=str(row["sender"]),
            recipient=str(row["recipient"]),
            payload=bytes(row["payload"]),
            intent=str(row["intent"]),
            lifecycle=DeliveryLifecycle(str(row["lifecycle"])),
            created_at_ms=int(row["created_at_ms"]),
            idempotency_key=row["idempotency_key"],
            origin_node=(
                str(row["origin_node"])
                if "origin_node" in row.keys() and row["origin_node"] is not None
                else None
            ),
        )

    def pending_all(self) -> tuple[InboxMessage, ...]:
        return self._messages(
            """SELECT * FROM inbox
               WHERE consumed = 0
                 AND NOT EXISTS (
                     SELECT 1 FROM harness_failure_settlements
                      WHERE harness_failure_settlements.message_id = inbox.message_id
                        AND harness_failure_settlements.terminal = 1
                 )
               ORDER BY arrival_id""",
            (),
        )

    def next(self, recipient: str) -> InboxMessage | None:
        messages = self._messages(
            """SELECT * FROM inbox
               WHERE recipient = ? AND consumed = 0
                 AND NOT EXISTS (
                     SELECT 1 FROM harness_failure_settlements
                      WHERE harness_failure_settlements.message_id = inbox.message_id
                        AND harness_failure_settlements.terminal = 1
                 )
               ORDER BY arrival_id LIMIT 1""",
            (recipient,),
        )
        return messages[0] if messages else None

    def pending_messages(self, recipient: str) -> tuple[InboxMessage, ...]:
        return self._messages(
            """SELECT * FROM inbox
               WHERE recipient = ? AND consumed = 0
                 AND NOT EXISTS (
                     SELECT 1 FROM harness_failure_settlements
                      WHERE harness_failure_settlements.message_id = inbox.message_id
                        AND harness_failure_settlements.terminal = 1
                 )
               ORDER BY arrival_id""",
            (recipient,),
        )

    def dispatchable_messages(
        self, recipient: str, *, now_ms: int | None = None
    ) -> tuple[InboxMessage, ...]:
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        return self._messages(
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
        )

    def harness_failure_settlement(
        self, message_id: str
    ) -> HarnessFailureSettlement | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM harness_failure_settlements WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return None if row is None else self._settlement_row(row)

    def terminal_failure_settlements(
        self, *, since_ms: int
    ) -> tuple[HarnessFailureSettlement, ...]:
        """Durable terminal tombstones from ``since_ms`` onward.

        Read by the daemon's restart recovery: a sender notice that could not
        be submitted before a crash is re-derived from these rows, so the owed
        fact outlives the process instead of dying with an in-memory retry.
        """

        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM harness_failure_settlements
                   WHERE terminal = 1 AND updated_at_ms >= ?
                   ORDER BY updated_at_ms""",
                (since_ms,),
            ).fetchall()
        return tuple(self._settlement_row(row) for row in rows)

    @staticmethod
    def _settlement_row(row: sqlite3.Row) -> HarnessFailureSettlement:
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

    def harness_failure_attempts(
        self, message_id: str
    ) -> tuple[HarnessFailureAttempt, ...]:
        with self._connect() as connection:
            rows = connection.execute(
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

    def harness_failure_original(self, message_id: str) -> InboxMessage | None:
        messages = self._messages(
            "SELECT * FROM inbox WHERE message_id = ? LIMIT 1",
            (message_id,),
        )
        return messages[0] if messages else None

    def system_notices(self, recipient: str) -> tuple[InboxMessage, ...]:
        return self._messages(
            "SELECT * FROM system_notices WHERE recipient = ? "
            "ORDER BY created_at_ms, rowid",
            (recipient,),
        )

    def list_progress_events(
        self,
        recipient: str,
        *,
        delivery_id: str | None = None,
    ) -> tuple[InboxMessage, ...]:
        if delivery_id is None:
            return self._messages(
                "SELECT * FROM progress_events WHERE recipient = ? "
                "ORDER BY created_at_ms, rowid",
                (recipient,),
            )
        return self._messages(
            "SELECT * FROM progress_events WHERE recipient = ? AND delivery_id = ? "
            "ORDER BY created_at_ms, rowid",
            (recipient, delivery_id),
        )

    def workflow_replies(self, recipient: str, conversation_id: str) -> tuple[InboxMessage, ...]:
        # Observation includes consumed rows and never fetches, ACKs or drains notices.
        return self._messages(
            "SELECT * FROM inbox WHERE recipient = ? AND conversation_id = ? AND intent = 'reply' "
            "ORDER BY created_at_ms DESC, arrival_id DESC LIMIT 101", (recipient, conversation_id),
        )

    def outbox_items(self) -> tuple[OutboxItem, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM outbox ORDER BY created_at_ms, rowid"
            ).fetchall()
        return tuple(
            OutboxItem(
                self._message(row),
                attempts=int(row["attempts"]),
                next_attempt_ms=int(row["next_attempt_ms"]),
                expires_at_ms=int(row["expires_at_ms"]),
            )
            for row in rows
        )

    def outbox_item(self, message_id: str) -> OutboxItem:
        for item in self.outbox_items():
            if item.message.message_id == message_id:
                return item
        raise KeyError(message_id)

    def outbox_count(self) -> int:
        return self._count("outbox")

    def submission_result(
        self,
        correlation_id: str,
        message_id: str,
    ) -> SubmissionResult | None:
        """Read one actor-owned durable submit receipt without mutating it."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT message_id, accepted, queued, code, custody_mailbox"
                " FROM actor_submission_receipts WHERE correlation_id = ?",
                (correlation_id,),
            ).fetchone()
        if row is None:
            return None
        if str(row["message_id"]) != message_id:
            raise RuntimeError("durable submission receipt message mismatch")
        return SubmissionResult(
            message_id,
            bool(row["accepted"]),
            queued=bool(row["queued"]),
            code=(str(row["code"]) if row["code"] is not None else None),
            custody_mailbox=(
                str(row["custody_mailbox"])
                if row["custody_mailbox"] is not None
                else None
            ),
        )

    def custody_count(self) -> int:
        return self._count("custody")

    def dlq_count(self) -> int:
        return self._count("dlq")

    def pending_count(self, recipient: str) -> int:
        with self._connect() as connection:
            return int(
                connection.execute(
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

    def pending_recipient_counts(self) -> tuple[tuple[str, int], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT recipient, COUNT(*) AS count FROM inbox
                   WHERE consumed = 0
                     AND NOT EXISTS (
                         SELECT 1 FROM harness_failure_settlements
                          WHERE harness_failure_settlements.message_id = inbox.message_id
                            AND harness_failure_settlements.terminal = 1
                     )
                   GROUP BY recipient ORDER BY recipient"""
            ).fetchall()
        return tuple((str(row["recipient"]), int(row["count"])) for row in rows)

    def unfetched_recipient_stats(
        self, *, hold_ttl_ms: int = DEFAULT_HOLD_TTL_MS
    ) -> tuple[tuple[str, int, int], ...]:
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

        ⛔ And a MANAGED worker never carries ``fetched_at_ms`` at all -- not
        "usually not", never.  Its mail arrives by the daemon pushing it
        (``dispatchable_messages`` is a pure SELECT, authority.py:150), and
        nothing schedules ``harness_read`` on its behalf: ``serve_worker_stdio``
        states that "a managed worker does NOT register an interactive session
        or run a wake loop", so that call happens when the worker's agent
        decides to make it, or not at all.  Managed workers therefore enter
        this reading only through the ``refresh_hold`` fingerprint above.

        ⇒ ``fetched_at_ms`` is evidence about ONE dispatch path.  It says
        nothing about whether a managed worker is alive, listening, or
        reachable -- and on 2026-09-18 it misled two readers in one night,
        first as "is anybody collecting their mail", then as "has this worker
        gone deaf".
        """

        with self._connect() as connection:
            rows = connection.execute(
                """SELECT recipient, COUNT(*) AS count,
                          MIN(received_at_ms) AS oldest
                     FROM inbox
                    WHERE consumed = 0
                      AND fetched_at_ms IS NULL
                      AND expires_at_ms <= received_at_ms + ?
                      AND NOT EXISTS (
                          SELECT 1 FROM harness_failure_settlements
                           WHERE harness_failure_settlements.message_id = inbox.message_id
                             AND harness_failure_settlements.terminal = 1
                      )
                    GROUP BY recipient ORDER BY recipient""",
                (hold_ttl_ms,),
            ).fetchall()
        return tuple(
            (str(row["recipient"]), int(row["count"]), int(row["oldest"]))
            for row in rows
        )

    def pending_recipient_stats(self) -> tuple[tuple[str, int, int], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT recipient, COUNT(*) AS count,
                          MIN(received_at_ms) AS oldest
                     FROM inbox
                    WHERE consumed = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM harness_failure_settlements
                           WHERE harness_failure_settlements.message_id = inbox.message_id
                             AND harness_failure_settlements.terminal = 1
                      )
                    GROUP BY recipient ORDER BY recipient"""
            ).fetchall()
        return tuple(
            (str(row["recipient"]), int(row["count"]), int(row["oldest"]))
            for row in rows
        )

    def has_fetched(self, message_id: str) -> bool:
        return self._exists(
            "SELECT 1 FROM inbox WHERE message_id = ? AND fetched_at_ms IS NOT NULL",
            (message_id,),
        )

    def is_acknowledged(self, message_id: str) -> bool:
        return self._exists(
            "SELECT 1 FROM inbox WHERE message_id = ? AND consumed = 1",
            (message_id,),
        )

    def has_received(self, message_id: str) -> bool:
        return self._exists(
            "SELECT 1 FROM inbox WHERE message_id = ? UNION ALL "
            "SELECT 1 FROM dedup WHERE dedup_key = ? LIMIT 1",
            (message_id, f"msgid:{message_id}"),
        )

    def has_custody(self, message_id: str) -> bool:
        return self._exists(
            "SELECT 1 FROM custody WHERE message_id = ?",
            (message_id,),
        )

    def held_expiry_ms(self, message_id: str) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT expires_at_ms FROM outbox WHERE message_id = ? "
                "UNION ALL SELECT expires_at_ms FROM custody WHERE message_id = ? "
                "LIMIT 1",
                (message_id, message_id),
            ).fetchone()
        return None if row is None else int(row[0])

    def consumption_state(self, message_id: str) -> ConsumptionState:
        with self._connect() as connection:
            row = connection.execute(
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
                return (
                    ConsumptionState.CONSUMED
                    if bool(row["consumed"])
                    else ConsumptionState.PENDING
                )
            status = connection.execute(
                "SELECT state FROM delivery_status WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        if status is None:
            return ConsumptionState.UNKNOWN
        return (
            ConsumptionState.CONSUMED
            if status["state"] == TerminalState.FETCHED.value
            else ConsumptionState.EXPIRED
        )

    def delivery_status_for_recipient(
        self, recipient: str, message_id: str
    ) -> DeliveryStatus | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_status WHERE message_id = ? AND recipient = ?",
                (message_id, recipient),
            ).fetchone()
        return None if row is None else self._status(row)

    def delivery_status_records(
        self,
        sender: str,
        *,
        message_id: str | None = None,
        limit: int = 500,
    ) -> tuple[DeliveryStatus, ...]:
        with self._connect() as connection:
            if message_id is None:
                rows = connection.execute(
                    "SELECT * FROM delivery_status WHERE sender = ? "
                    "ORDER BY recorded_at_ms DESC, message_id LIMIT ?",
                    (sender, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM delivery_status WHERE sender = ? AND message_id = ?",
                    (sender, message_id),
                ).fetchall()
        return tuple(self._status(row) for row in rows)

    def _messages(
        self,
        query: str,
        params: tuple[object, ...],
    ) -> tuple[InboxMessage, ...]:
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(self._message(row) for row in rows)

    def _count(self, table: str) -> int:
        if table not in {"outbox", "custody", "dlq"}:
            raise ValueError(table)
        with self._connect() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _exists(self, query: str, params: tuple[object, ...]) -> bool:
        with self._connect() as connection:
            return connection.execute(query, params).fetchone() is not None

    @staticmethod
    def _status(row: sqlite3.Row) -> DeliveryStatus:
        return DeliveryStatus(
            message_id=str(row["message_id"]),
            sender=str(row["sender"]),
            recipient=str(row["recipient"]),
            state=TerminalState(str(row["state"])),
            holder=str(row["holder"]),
            reason=str(row["reason"]),
            recorded_at_ms=int(row["recorded_at_ms"]),
            conversation_id=str(row["conversation_id"] or ""),
            idempotency_key=row["idempotency_key"],
        )


class DeliveryStatusProjection:
    def __init__(self, reads: InboxReadProjection) -> None:
        self._reads = reads

    def for_sender(
        self,
        sender: str,
        *,
        message_id: str | None = None,
        limit: int = 500,
    ) -> tuple[DeliveryStatus, ...]:
        return self._reads.delivery_status_records(
            sender,
            message_id=message_id,
            limit=limit,
        )


class ActorAlarmEmitter:
    """``AlarmEmitter.emit`` compatibility backed by typed actor commands."""

    def __init__(self, authority: DeliveryCustodyFacade) -> None:
        self._authority = authority

    def emit(
        self,
        alarm: Alarm,
        *,
        delivery: AlarmDelivery | None = None,
        terminal: bool = True,
        throttle: bool = True,
    ) -> AlarmResult:
        if delivery is not None:
            return AlarmResult("failed", alarm.audience)
        try:
            return self._authority.emit_alarm(
                alarm,
                terminal=terminal,
                throttle=throttle,
            )
        except (NameError, ImportError):
            raise
        except Exception:
            return AlarmResult("failed", alarm.audience)


class DeliveryCustodyFacade:
    """Compatibility/system-edge API; every mutation is an actor command."""

    def __init__(
        self,
        coordinator: DeliveryCustodyCoordinator,
        database: Path,
        *,
        timeout: float = DELIVERY_CUSTODY_CALL_TIMEOUT_SECONDS,
    ) -> None:
        self._coordinator = coordinator
        self._reads = InboxReadProjection(database)
        self._timeout = timeout
        self.delivery_status = DeliveryStatusProjection(self._reads)
        self.alarm_emitter = ActorAlarmEmitter(self)

    @property
    def generation(self) -> int:
        return self._coordinator.generation

    @property
    def version(self) -> int:
        return self._coordinator.version

    def _correlation(self, operation: str) -> str:
        return f"inbox:{operation}:{uuid4().hex}"

    def _call(self, command: object, expected: type[object]) -> object:
        return self._coordinator.call(  # type: ignore[arg-type]
            command,  # type: ignore[arg-type]
            expected,
            timeout=self._timeout,
        )

    @staticmethod
    def _submission(value: SubmissionProjection) -> SubmissionResult:
        return SubmissionResult(
            value.message_id,
            value.accepted,
            queued=value.queued,
            code=value.code,
            custody_mailbox=value.custody_mailbox,
        )

    def submit(
        self,
        message: InboxMessage,
        *,
        now_ms: int | None = None,
        defer_direct: bool = False,
        correlation_id: str | None = None,
    ) -> SubmissionResult:
        # Correlated reply messages retain the same message id across public
        # message.reply retries.  Use it as the durable command identity so a
        # caller timeout or actor-generation change rejoins the existing
        # receipt rather than launching the native reply again.
        correlation = correlation_id
        if (
            correlation is None
            and message.intent == "reply"
            and isinstance(message.idempotency_key, str)
            and message.idempotency_key.startswith("reply:")
        ):
            correlation = f"inbox:reply-submit:{message.message_id}"
        correlation = correlation or self._correlation("submit")
        try:
            event = self._call(
                SubmitMessageCommand(
                    correlation,
                    message,
                    now_ms,
                    defer_direct,
                ),
                SubmissionCompleted,
            )
        except InboxAuthorityTimeout:
            if not correlation.startswith("inbox:reply-submit:"):
                raise
            # The transport timeout is unchanged.  A reply may already have
            # succeeded while its actor completion is crossing the mailbox;
            # join only that same durable receipt for a small handoff grace.
            # Never resubmit the command or repeat external I/O.
            deadline = (
                time.monotonic() + _REPLY_COMPLETION_HANDOFF_GRACE_SECONDS
            )
            while True:
                settled = self._reads.submission_result(
                    correlation,
                    message.message_id,
                )
                if settled is not None:
                    return settled
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.01, remaining))
        assert isinstance(event, SubmissionCompleted)
        return self._submission(event.result)

    def receive(
        self,
        message: InboxMessage,
        *,
        now_ms: int | None = None,
    ) -> ReceiveResult:
        event = self._call(
            ReceiveMessageCommand(self._correlation("receive"), message, now_ms),
            ReceiveCompleted,
        )
        assert isinstance(event, ReceiveCompleted)
        return event.result

    def ack(self, recipient: str, message_id: str) -> AckResult:
        event = self._call(
            AcknowledgeMessageCommand(
                self._correlation("ack"), recipient, message_id
            ),
            AcknowledgeCompleted,
        )
        assert isinstance(event, AcknowledgeCompleted)
        return event.result

    def refresh_hold(self, message_id: str, *, now_ms: int | None = None) -> bool:
        return self._bool(
            RefreshHoldCommand(
                self._correlation("refresh-hold"), message_id, now_ms
            )
        )

    def fail(self, recipient: str, message_id: str, detail: str) -> FailureResult:
        event = self._call(
            FailMessageCommand(
                self._correlation("fail"), recipient, message_id, detail
            ),
            FailureMutationCompleted,
        )
        assert isinstance(event, FailureMutationCompleted)
        return event.result

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
        event = self._call(
            SettleHarnessFailureCommand(
                self._correlation("settle-harness-failure"),
                recipient,
                message_id,
                failure_code,
                permanent,
                max_attempts,
                backoff_ms,
                now_ms,
            ),
            HarnessFailureSettled,
        )
        assert isinstance(event, HarnessFailureSettled)
        return event.result

    def harness_failure_settlement(
        self, message_id: str
    ) -> HarnessFailureSettlement | None:
        return self._reads.harness_failure_settlement(message_id)

    def harness_failure_attempts(
        self, message_id: str
    ) -> tuple[HarnessFailureAttempt, ...]:
        return self._reads.harness_failure_attempts(message_id)

    def harness_failure_original(self, message_id: str) -> InboxMessage | None:
        return self._reads.harness_failure_original(message_id)

    def terminal_failure_settlements(
        self, *, since_ms: int
    ) -> tuple[HarnessFailureSettlement, ...]:
        return self._reads.terminal_failure_settlements(since_ms=since_ms)

    def accept_custody(
        self,
        message: InboxMessage,
        *,
        mailbox_node: str,
        now_ms: int | None = None,
    ) -> AckResult:
        event = self._call(
            AcceptCustodyCommand(
                self._correlation("accept-custody"),
                message,
                mailbox_node,
                now_ms,
            ),
            AcknowledgeCompleted,
        )
        assert isinstance(event, AcknowledgeCompleted)
        return event.result

    def retry_due(self, *, now_ms: int | None = None) -> list[SubmissionResult]:
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        event = self._call(
            RetryDueCommand(
                self._correlation("retry"), self.generation, self.version, now
            ),
            RetryIoCompleted,
        )
        assert isinstance(event, RetryIoCompleted)
        results = [self._submission(item) for item in event.results]
        results.extend(self.retry_custody_due(now_ms=now))
        return results

    def retry_custody_due(
        self, *, now_ms: int | None = None
    ) -> list[SubmissionResult]:
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        event = self._call(
            RetryCustodyCommand(
                self._correlation("retry-custody"),
                self.generation,
                self.version,
                now,
            ),
            SubmissionBatchCompleted,
        )
        assert isinstance(event, SubmissionBatchCompleted)
        return [self._submission(item) for item in event.results]

    def retire_outbox_receipt(
        self,
        sender: str,
        message_id: str,
        *,
        now_ms: int | None = None,
    ) -> bool:
        return self._bool(
            RetireOutboxReceiptCommand(
                self._correlation("retire-receipt"), sender, message_id, now_ms
            )
        )

    def receive_system_notice(self, notice: InboxMessage) -> bool:
        return self._bool(
            ReceiveSystemNoticeCommand(
                self._correlation("receive-notice"), notice
            )
        )

    def drain_system_notices(self, recipient: str) -> tuple[InboxMessage, ...]:
        return self._messages(
            DrainSystemNoticesCommand(
                self._correlation("drain-notices"), recipient
            )
        )

    def dismiss_system_notice(self, message_id: str) -> bool:
        return self._bool(
            DismissSystemNoticeCommand(
                self._correlation("dismiss-notice"), message_id
            )
        )

    def submit_progress_event(self, event: ProgressEvent, *, recipient: str) -> bool:
        return self._bool(
            SubmitProgressCommand(
                self._correlation("submit-progress"), event, recipient
            )
        )

    def receive_progress_event(self, message: InboxMessage) -> bool:
        return self._bool(
            ReceiveProgressCommand(
                self._correlation("receive-progress"), message
            )
        )

    def emit_alarm(
        self,
        alarm: Alarm,
        *,
        terminal: bool = True,
        throttle: bool = True,
    ) -> AlarmResult:
        event = self._call(
            EmitAlarmCommand(
                self._correlation("emit-alarm"),
                alarm,
                terminal,
                throttle,
            ),
            AlarmCompleted,
        )
        assert isinstance(event, AlarmCompleted)
        return event.result

    def fetch_pending(
        self,
        recipient: str,
        *,
        now_ms: int | None = None,
    ) -> tuple[InboxMessage, ...]:
        return self._messages(
            FetchPendingCommand(self._correlation("fetch"), recipient, now_ms)
        )

    def prune_inbox(
        self, *, now_ms: int | None = None
    ) -> tuple[InboxPruneItem, ...]:
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        event = self._call(
            PruneInboxCommand(
                self._correlation("prune-inbox"),
                self.generation,
                self.version,
                now,
            ),
            InboxPruneCompleted,
        )
        assert isinstance(event, InboxPruneCompleted)
        return event.items

    def prune_outbox(
        self,
        *,
        undeliverable: Callable[[str], bool],
        unresolvable: Callable[[str], bool] | None = None,
        now_ms: int | None = None,
        dry_run: bool = False,
    ) -> tuple[OutboxPruneItem, ...]:
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        recipients = {item.message.recipient for item in self._reads.outbox_items()}
        dead = tuple(sorted(recipient for recipient in recipients if undeliverable(recipient)))
        missing = tuple(
            sorted(
                recipient
                for recipient in recipients
                if unresolvable is not None and unresolvable(recipient)
            )
        )
        event = self._call(
            PruneOutboxCommand(
                self._correlation("prune-outbox"),
                self.generation,
                self.version,
                now,
                dead,
                missing,
                dry_run,
            ),
            OutboxPruneCompleted,
        )
        assert isinstance(event, OutboxPruneCompleted)
        return event.items

    def shutdown(self) -> None:
        event = self._call(
            CloseInboxCommand(self._correlation("close")),
            InboxClosed,
        )
        assert isinstance(event, InboxClosed)
        report = self._coordinator.drain(self._timeout)
        if not report.complete:
            raise RuntimeError("inbox custody did not drain before its deadline")

    def close(self) -> None:
        """Context-manager compatibility; production composition uses shutdown."""

        self.shutdown()

    def __enter__(self) -> DeliveryCustodyFacade:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _bool(self, command: object) -> bool:
        event = self._call(command, BoolMutationCompleted)
        assert isinstance(event, BoolMutationCompleted)
        return event.result

    def _messages(self, command: object) -> tuple[InboxMessage, ...]:
        event = self._call(command, MessagesMutationCompleted)
        assert isinstance(event, MessagesMutationCompleted)
        return event.messages

    def consumption_state(self, message_id: str) -> ConsumptionState:
        return self._reads.consumption_state(message_id)

    def pending_all(self) -> tuple[InboxMessage, ...]:
        return self._reads.pending_all()

    def next(self, recipient: str) -> InboxMessage | None:
        return self._reads.next(recipient)

    def pending_messages(self, recipient: str) -> tuple[InboxMessage, ...]:
        return self._reads.pending_messages(recipient)

    def dispatchable_messages(
        self, recipient: str, *, now_ms: int | None = None
    ) -> tuple[InboxMessage, ...]:
        return self._reads.dispatchable_messages(recipient, now_ms=now_ms)

    def system_notices(self, recipient: str) -> tuple[InboxMessage, ...]:
        return self._reads.system_notices(recipient)

    def list_progress_events(
        self, recipient: str, *, delivery_id: str | None = None
    ) -> tuple[InboxMessage, ...]:
        return self._reads.list_progress_events(recipient, delivery_id=delivery_id)

    def workflow_replies(self, recipient: str, conversation_id: str) -> tuple[InboxMessage, ...]:
        return self._reads.workflow_replies(recipient, conversation_id)

    def outbox_item(self, message_id: str) -> OutboxItem:
        return self._reads.outbox_item(message_id)

    def outbox_items(self) -> tuple[OutboxItem, ...]:
        return self._reads.outbox_items()

    def outbox_count(self) -> int:
        return self._reads.outbox_count()

    def custody_count(self) -> int:
        return self._reads.custody_count()

    def dlq_count(self) -> int:
        return self._reads.dlq_count()

    def pending_count(self, recipient: str) -> int:
        return self._reads.pending_count(recipient)

    def pending_recipient_counts(self) -> tuple[tuple[str, int], ...]:
        return self._reads.pending_recipient_counts()

    def pending_recipient_stats(self) -> tuple[tuple[str, int, int], ...]:
        return self._reads.pending_recipient_stats()

    def unfetched_recipient_stats(self) -> tuple[tuple[str, int, int], ...]:
        return self._reads.unfetched_recipient_stats()

    def has_fetched(self, message_id: str) -> bool:
        return self._reads.has_fetched(message_id)

    def is_acknowledged(self, message_id: str) -> bool:
        return self._reads.is_acknowledged(message_id)

    def has_received(self, message_id: str) -> bool:
        return self._reads.has_received(message_id)

    def has_custody(self, message_id: str) -> bool:
        return self._reads.has_custody(message_id)

    def held_expiry_ms(self, message_id: str) -> int | None:
        return self._reads.held_expiry_ms(message_id)

    def delivery_status_records(
        self, sender: str, *, message_id: str | None = None, limit: int = 500
    ) -> tuple[DeliveryStatus, ...]:
        return self._reads.delivery_status_records(
            sender, message_id=message_id, limit=limit
        )

    def delivery_status_for_recipient(
        self, recipient: str, message_id: str
    ) -> DeliveryStatus | None:
        return self._reads.delivery_status_for_recipient(recipient, message_id)
