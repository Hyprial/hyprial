from __future__ import annotations
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from hyprial.daemon.impl.inbox.contracts.api  import (
    DeliveryLifecycle,
    HarnessFailureAttempt,
    HarnessFailureSettlement,
    InboxMessage,
    OutboxItem,
    SubmissionResult,
)
from hyprial.daemon.impl.inbox.links.pull  import DEFAULT_HOLD_TTL_MS, DeliveryStatus, TerminalState
from hyprial.daemon.impl.inbox.service.state  import ConsumptionState

_REPLY_COMPLETION_HANDOFF_GRACE_SECONDS = 0.5
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
            expires_at_ms=(
                int(row["message_expires_at_ms"])
                if "message_expires_at_ms" in row.keys()
                and row["message_expires_at_ms"] is not None
                else None
            ),
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

    def pending_messages(
        self,
        recipient: str,
        *,
        now_ms: int | None = None,
    ) -> tuple[InboxMessage, ...]:
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        return self._messages(
            """SELECT * FROM inbox
               WHERE recipient = ? AND consumed = 0
                 AND (expires_at_ms IS NULL OR expires_at_ms > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM harness_failure_settlements
                      WHERE harness_failure_settlements.message_id = inbox.message_id
                        AND harness_failure_settlements.terminal = 1
                 )
               ORDER BY arrival_id""",
            (recipient, now),
        )

    def dispatchable_messages(
        self, recipient: str, *, now_ms: int | None = None
    ) -> tuple[InboxMessage, ...]:
        now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
        return self._messages(
            """SELECT * FROM inbox
               WHERE recipient = ? AND consumed = 0
                 AND (expires_at_ms IS NULL OR expires_at_ms > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM harness_failure_settlements
                      WHERE harness_failure_settlements.message_id = inbox.message_id
                        AND (
                            harness_failure_settlements.terminal = 1
                            OR harness_failure_settlements.next_attempt_ms > ?
                        )
                 )
               ORDER BY arrival_id""",
            (recipient, now, now),
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

    def outbox_recipient_page(
        self, *, after: str | None = None, limit: int = 64
    ) -> tuple[str, ...]:
        """Bounded stable recipient page for overflow wake recovery."""

        if limit < 1 or limit > 256:
            raise ValueError("outbox recipient page limit must be in 1..256")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT recipient FROM outbox
                     WHERE (? IS NULL OR recipient > ?)
                     ORDER BY recipient LIMIT ?""",
                (after, after, limit),
            ).fetchall()
        return tuple(str(row["recipient"]) for row in rows)

    def outbox_item(self, message_id: str) -> OutboxItem:
        for item in self.outbox_items():
            if item.message.message_id == message_id:
                return item
        raise KeyError(message_id)

    def outbox_count(self) -> int:
        return self._count("outbox")

    def next_retry_due_ms(
        self,
        *,
        exclude_message_ids: frozenset[str] = frozenset(),
    ) -> int | None:
        """Earliest scheduler key across outbox and transferred custody."""

        excluded = tuple(sorted(exclude_message_ids))
        placeholders = ", ".join("?" for _ in excluded)
        predicate = (
            f" WHERE message_id NOT IN ({placeholders})"  # noqa: S608
            if excluded
            else ""
        )
        with self._connect() as connection:
            row = connection.execute(
                f"""SELECT MIN(next_attempt_ms) AS due_ms
                     FROM (
                         SELECT next_attempt_ms FROM outbox{predicate}
                         UNION ALL
                         SELECT next_attempt_ms FROM custody{predicate}
                     )""",  # noqa: S608
                (*excluded, *excluded),
            ).fetchone()
        return None if row is None or row["due_ms"] is None else int(row["due_ms"])

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

    def has_pending_work(self, recipient: str) -> bool:
        """Whether inbox or custody owns unconsumed work for this recipient."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM inbox WHERE recipient=? AND consumed=0) "
                "OR EXISTS(SELECT 1 FROM custody WHERE recipient=?)",
                (recipient, recipient),
            ).fetchone()
        return bool(row[0])

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
