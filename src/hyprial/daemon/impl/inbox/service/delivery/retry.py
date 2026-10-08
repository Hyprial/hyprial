from __future__ import annotations
import sqlite3
from hyprial.daemon import Alarm, audience_for_sender
from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.inbox.contracts.api  import (
    AckResult,
    DeliveryLifecycle,
    FailureResult,
    HarnessFailureAttempt,
    HarnessFailureSettlement,
    InboxMessage,
    OutboxItem,
    SubmissionResult,
)
from hyprial.daemon.impl.inbox.links.pull  import (
    HoldReason,
    TerminalState,
)
from hyprial.daemon.impl.inbox.service.policy import (
    DELIVERY_RETRY_CLAIM_LIMIT,
    online_retry_next_attempt_ms,
)
from hyprial.daemon.impl.inbox.service.synchronization import _synchronized

"""SQLite-backed outbox, inbox, deduplication, FIFO, DLQ and custody state."""

class InboxServiceRetryMixin:
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
        """Refresh an ordinary hold without replacing a producer deadline.

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
                """UPDATE inbox
                      SET expires_at_ms = COALESCE(message_expires_at_ms, ?)
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
                    "UPDATE inbox SET expires_at_ms = "
                    "CASE WHEN message_expires_at_ms IS NULL "
                    "THEN MAX(expires_at_ms, ?) ELSE message_expires_at_ms END "
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
                       accepted_at_ms, message_expires_at_ms, expires_at_ms,
                       next_attempt_ms
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    # Ordinary custody uses this mailbox's clock; an explicit
                    # producer deadline remains fixed across the transfer.
                    now,
                    message.expires_at_ms,
                    (
                        message.expires_at_ms
                        if message.expires_at_ms is not None
                        else now + self.custody_ttl_ms
                    ),
                    now,
                ),
            )
        self._enforce_hold_capacity("custody", now)
        return AckResult(message.message_id, True)
    @_synchronized
    def retry_custody_due(self, *, now_ms: int | None = None) -> list[SubmissionResult]:
        now = self._now_ms() if now_ms is None else now_ms
        rows = self._db.execute(
            "SELECT * FROM custody WHERE next_attempt_ms <= ? "
            "ORDER BY accepted_at_ms, rowid LIMIT ?",
            (now, DELIVERY_RETRY_CLAIM_LIMIT),
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
                self._custody_to_dlq(row, "TTL_EXPIRED", now)
                outcomes.append(
                    SubmissionResult(message.message_id, False, code="TTL_EXPIRED")
                )
            elif not self._transport.is_online(message.recipient):
                with self._db:
                    self._db.execute(
                        "UPDATE custody SET next_attempt_ms = expires_at_ms "
                        "WHERE message_id = ?",
                        (message.message_id,),
                    )
                outcomes.append(
                    SubmissionResult(message.message_id, True, queued=True)
                )
            elif self._transport.deliver(message):
                # Same correction as ``_attempt_direct``: ``now`` predates the
                # blocking delivery, so the record would claim to have been
                # written before the event it records.
                confirmed_at_ms = self._now_ms()
                with self._db:
                    self._record_terminal(
                        message,
                        state=TerminalState.FETCHED,
                        reason=HoldReason.ACK_RECEIVED,
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
            else:
                attempts = int(row["attempts"]) + 1
                next_attempt_ms = online_retry_next_attempt_ms(
                    message_id=message.message_id,
                    attempts=attempts,
                    now_ms=now,
                    expires_at_ms=int(row["expires_at_ms"]),
                )
                with self._db:
                    self._db.execute(
                        """UPDATE custody
                              SET attempts = ?,
                                  next_attempt_ms = MIN(expires_at_ms, ?)
                            WHERE message_id = ?""",
                        (attempts, next_attempt_ms, message.message_id),
                    )
                outcomes.append(
                    SubmissionResult(message.message_id, True, queued=True)
                )
        return outcomes

    @_synchronized
    def next_retry_due_ms(self) -> int | None:
        """Earliest scheduler key across both durable custody stores."""

        row = self._db.execute(
            """SELECT MIN(next_attempt_ms) AS due_ms
                 FROM (
                     SELECT next_attempt_ms FROM outbox
                     UNION ALL
                     SELECT next_attempt_ms FROM custody
                 )"""
        ).fetchone()
        return None if row is None or row["due_ms"] is None else int(row["due_ms"])
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
            expires_at_ms=(
                int(row["message_expires_at_ms"])
                if "message_expires_at_ms" in row.keys()
                and row["message_expires_at_ms"] is not None
                else None
            ),
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
                reason=HoldReason.ACK_RECEIVED,
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
        with self._db:
            self._outbox_to_dlq_locked(
                row,
                reason,
                now_ms,
                local_notice=local_notice,
            )
        self._finish_outbox_dlq(row, reason, now_ms, emit_failure=emit_failure)

    def _outbox_to_dlq_locked(
        self,
        row: sqlite3.Row,
        reason: str,
        now_ms: int,
        *,
        local_notice: InboxMessage | None = None,
    ) -> None:
        """Apply the durable DLQ transition inside the caller's transaction."""

        message = self._row_message(row)
        self._record_terminal(
            message,
            state=TerminalState.EXPIRED,
            reason=reason,
            now_ms=now_ms,
        )
        self._db.execute(
            "INSERT INTO dlq(message_id, owner, payload, reason, failed_at_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["message_id"], row["sender"], row["payload"], reason, now_ms),
        )
        self._db.execute(
            "DELETE FROM outbox WHERE message_id = ?", (row["message_id"],)
        )
        if local_notice is not None:
            self._persist_system_notice_locked(local_notice)

    def _finish_outbox_dlq(
        self,
        row: sqlite3.Row,
        reason: str,
        now_ms: int,
        *,
        emit_failure: bool = True,
    ) -> None:
        """Run non-transactional observations after a DLQ transition commits."""

        message = self._row_message(row)
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
