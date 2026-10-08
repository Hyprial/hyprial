from __future__ import annotations
from collections.abc import Callable
from typing import Self
from hyprial.daemon.impl.inbox.contracts.api  import (
    InboxMessage,
    InboxPruneItem,
    OutboxPruneItem,
)
from hyprial.daemon.impl.inbox.links.pull  import (
    DeliveryStatus,
    DeliveryStatusStore,
)
from hyprial.daemon.impl.inbox.service.delivery.notices import _ExpiredSenderNotice
from hyprial.daemon.impl.inbox.service.synchronization import _synchronized

"""SQLite-backed outbox, inbox, deduplication, FIFO, DLQ and custody state."""

class InboxServiceProjectionMixin:
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
                reason = "TTL_EXPIRED"
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
        reason ``TERMINAL_SETTLED`` instead of ``TTL_EXPIRED``.  The
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
        items, notices = self._prune_inbox_state(now)
        for notice in notices:
            self._deliver_expired_sender_notice(notice)
        return items
    def _prune_inbox_state(
        self, now_ms: int
    ) -> tuple[tuple[InboxPruneItem, ...], tuple[_ExpiredSenderNotice, ...]]:
        """Commit one inbox sweep and return its best-effort notice effects."""

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
            (now_ms,),
        ).fetchall()
        if not rows:
            return (), ()
        notices = tuple(
            notice
            for row in rows
            if not row["settled_terminal"]
            and (notice := self._expired_sender_notice(row, now_ms)) is not None
        )
        with self._db:
            self._db.execute(
                """DELETE FROM inbox
                   WHERE consumed = 0
                     AND expires_at_ms IS NOT NULL
                     AND expires_at_ms <= ?""",
                (now_ms,),
            )
        items = tuple(
            InboxPruneItem(
                message_id=str(row["message_id"]),
                recipient=str(row["recipient"]),
                reason=(
                    "TERMINAL_SETTLED"
                    if row["settled_terminal"]
                    else "TTL_EXPIRED"
                ),
                created_at_ms=int(row["created_at_ms"]),
                received_at_ms=int(row["received_at_ms"]),
            )
            for row in rows
        )
        return items, notices
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
    def pending_work_recipients(self) -> frozenset[str]:
        """Payload-free inbox/custody recipients, read only by the state owner."""

        rows = self._db.execute(
            "SELECT recipient FROM inbox WHERE consumed=0 "
            "UNION SELECT recipient FROM custody"
        ).fetchall()
        return frozenset(str(row["recipient"]) for row in rows)
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
                 AND (message_expires_at_ms IS NOT NULL
                      OR expires_at_ms <= received_at_ms + ?)
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
    def pending_messages(
        self,
        recipient: str,
        *,
        now_ms: int | None = None,
    ) -> tuple[InboxMessage, ...]:
        now = self._now_ms() if now_ms is None else now_ms
        rows = self._db.execute(
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
                 AND (expires_at_ms IS NULL OR expires_at_ms > ?)
                 AND NOT EXISTS (
                     SELECT 1 FROM harness_failure_settlements
                      WHERE harness_failure_settlements.message_id = inbox.message_id
                        AND harness_failure_settlements.terminal = 1
                 )
               ORDER BY arrival_id""",
            (recipient, now),
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
    def delivery_status_for_recipient(
        self, recipient: str, message_id: str
    ) -> DeliveryStatus | None:
        """The terminal record of one message ``recipient`` received here."""

        return self._status.for_recipient(recipient, message_id)
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
