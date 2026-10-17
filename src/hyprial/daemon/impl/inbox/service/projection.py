from __future__ import annotations
import json
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


def _dispatch_reply_to(message: InboxMessage) -> str | None:
    """The reply association of a message, from its JSON body (review B1.2).

    Only a non-empty ``replyTo`` string counts.  A missing key, a wrong type
    or un-decodable body is no association -- never guessed from the text,
    the conversation id or the message id.
    """

    try:
        body = json.loads(message.payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    reply_to = body.get("replyTo")
    if isinstance(reply_to, str) and reply_to:
        return reply_to
    return None

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
            for row in rows:
                self._ledger_expire_inbound_locked(
                    str(row["recipient"]), str(row["message_id"])
                )
            self._db.execute(
                """DELETE FROM inbox
                   WHERE consumed = 0
                     AND expires_at_ms IS NOT NULL
                     AND expires_at_ms <= ?""",
                (now_ms,),
            )
        for row in rows:
            self._forget_admission(str(row["message_id"]))
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
                 AND (inbox.fetched_at_ms IS NULL
                      OR EXISTS (
                          SELECT 1 FROM harness_failure_settlements s
                           WHERE s.message_id = inbox.message_id
                             AND s.terminal = 0 AND s.next_attempt_ms <= ?
                      ))
               ORDER BY arrival_id""",
            (recipient, now, now, now),
        ).fetchall()
        return tuple(self._row_message(row) for row in rows)

    @_synchronized
    def fetched_between(
        self,
        recipient: str,
        *,
        since_ms: int,
        until_ms: int,
        since_inclusive: bool,
    ) -> tuple[str, ...]:
        """Message ids whose first fetch stamp falls in the window.

        ``fetched_at_ms`` is written once (``COALESCE``): this is the
        injection evidence for session carriers, not a reassembly cursor.
        """

        comparison = ">=" if since_inclusive else ">"
        rows = self._db.execute(
            f"""SELECT message_id FROM inbox
                 WHERE recipient = ? AND fetched_at_ms IS NOT NULL
                   AND fetched_at_ms {comparison} ? AND fetched_at_ms <= ?
                 ORDER BY arrival_id""",  # noqa: S608
            (recipient, since_ms, until_ms),
        ).fetchall()
        return tuple(str(row["message_id"]) for row in rows)

    @_synchronized
    def fetch_pending(
        self,
        recipient: str,
        *,
        now_ms: int | None = None,
        only: frozenset[str] | None = None,
        completed_turn: bool = False,
    ) -> tuple[InboxMessage, ...]:
        """Record one real pull and retire same-daemon sender holds.

        Background Channel observation must use ``pending_messages``.  This
        method is reserved for the public ``harness_read`` fetch boundary.
        ``only`` restricts the pull to a candidate set; it is only a filter,
        not an admission decision (that lives in ``fetch_pending_admitted``).

        ``completed_turn`` (WP5-A §3.3) additionally clears the row's
        non-terminal failure retries, in the same transaction as the stamp.
        """

        if completed_turn and only is None:
            raise ValueError("completed_turn requires an explicit candidate id")
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
        if only is not None:
            messages = tuple(m for m in messages if m.message_id in only)
        if not messages:
            return ()
        meta = self._inbound_ledger_meta(messages)
        with self._db:
            self._db.executemany(
                """UPDATE inbox SET fetched_at_ms = COALESCE(fetched_at_ms, ?)
                    WHERE message_id = ?""",
                ((now, message.message_id) for message in messages),
            )
            fresh = [
                message
                for message in messages
                if self._write_inbound_ledger_locked(
                    actor=recipient,
                    message=message,
                    received_at_ms=meta[message.message_id][0],
                    expires_at_ms=meta[message.message_id][1],
                )
            ]
            self._clear_nonterminal_failures(messages, completed_turn)
        for message in fresh:
            self._sink_injected(recipient, message, meta[message.message_id][0])
        for message in messages:
            self.retire_outbox_receipt(message.sender, message.message_id, now_ms=now)
        return messages

    @_synchronized
    def admitted_pending(
        self,
        recipient: str,
        *,
        now_ms: int | None = None,
        exclude: frozenset[str] = frozenset(),
    ) -> tuple[InboxMessage, ...]:
        """Live pending rows this recipient may inject (review B3).

        Runs on the inbox actor thread so every ``admit_dispatch`` call shares
        one W reservation authority.  ``exclude`` are rows the runtime already
        answered or is settling; they are dropped before the gate is asked, so
        they never newly occupy W.
        """

        now = self._now_ms() if now_ms is None else now_ms
        return self._admit_dispatch(
            self.pending_messages(recipient, now_ms=now), now, exclude
        )

    @_synchronized
    def admitted_dispatchable(
        self,
        recipient: str,
        *,
        now_ms: int | None = None,
        exclude: frozenset[str] = frozenset(),
    ) -> tuple[InboxMessage, ...]:
        """Dispatchable rows (failure backoff elapsed) this recipient may inject."""

        now = self._now_ms() if now_ms is None else now_ms
        return self._admit_dispatch(
            self.dispatchable_messages(recipient, now_ms=now), now, exclude
        )

    @_synchronized
    def fetch_pending_admitted(
        self,
        recipient: str,
        *,
        now_ms: int | None = None,
        only: frozenset[str] | None = None,
        completed_turn: bool = False,
    ) -> tuple[InboxMessage, ...]:
        """Admit, then stamp fetched, in one actor command (review B3.3).

        Re-reads live rows inside the actor, keeps only ``only`` candidates,
        asks the gate for each and stamps only what it admitted.  A row that
        expired, was consumed or was acked between the candidate query and this
        command is not live and therefore never returned or stamped.

        ``completed_turn`` (WP5-A §3.3): a successful headless turn also
        clears the row's non-terminal failure retries in the same transaction,
        so the durable fetched stamp never blocks a later retry.
        """

        if completed_turn and only is None:
            raise ValueError("completed_turn requires an explicit candidate id")
        now = self._now_ms() if now_ms is None else now_ms
        candidates = self.pending_messages(recipient, now_ms=now)
        if only is not None:
            candidates = tuple(m for m in candidates if m.message_id in only)
        admitted = self._admit_dispatch(candidates, now, frozenset())
        if not admitted:
            return ()
        meta = self._inbound_ledger_meta(admitted)
        with self._db:
            self._db.executemany(
                """UPDATE inbox SET fetched_at_ms = COALESCE(fetched_at_ms, ?)
                    WHERE message_id = ?""",
                ((now, message.message_id) for message in admitted),
            )
            fresh = [
                message
                for message in admitted
                if self._write_inbound_ledger_locked(
                    actor=recipient,
                    message=message,
                    received_at_ms=meta[message.message_id][0],
                    expires_at_ms=meta[message.message_id][1],
                )
            ]
            self._clear_nonterminal_failures(admitted, completed_turn)
        for message in fresh:
            self._sink_injected(recipient, message, meta[message.message_id][0])
        for message in admitted:
            self.retire_outbox_receipt(message.sender, message.message_id, now_ms=now)
        return admitted

    def _clear_nonterminal_failures(
        self, messages: tuple[InboxMessage, ...], completed_turn: bool
    ) -> None:
        """Drop retry eligibility for a successfully completed turn only.

        The append-only attempt log and any terminal tombstone stay; a FAILED
        turn never calls this.
        """

        if not completed_turn:
            return
        self._db.executemany(
            """DELETE FROM harness_failure_settlements
                WHERE message_id = ? AND terminal = 0""",
            ((message.message_id,) for message in messages),
        )

    def _inbound_ledger_meta(
        self, messages: tuple[InboxMessage, ...]
    ) -> dict[str, tuple[int, int]]:
        """Each message id's durable ``(received_at_ms, expires_at_ms)`` (B7)."""

        placeholders = ", ".join("?" for _ in messages)
        rows = self._db.execute(
            f"""SELECT message_id, received_at_ms, expires_at_ms FROM inbox
                 WHERE message_id IN ({placeholders})""",  # noqa: S608
            tuple(message.message_id for message in messages),
        ).fetchall()
        meta: dict[str, tuple[int, int]] = {}
        now = self._now_ms()
        for row in rows:
            received_at_ms = int(row["received_at_ms"])
            expires = row["expires_at_ms"]
            meta[str(row["message_id"])] = (
                received_at_ms,
                int(expires) if expires is not None else now + self.durable_ttl_ms,
            )
        return meta

    def _admit_dispatch(
        self,
        messages: tuple[InboxMessage, ...],
        now_ms: int,
        exclude: frozenset[str],
    ) -> tuple[InboxMessage, ...]:
        admission = self._link_admission
        admitted: list[InboxMessage] = []
        for message in messages:
            if message.message_id in exclude:
                continue
            if admission.dispatch_admitted(
                sender=message.sender,
                recipient=message.recipient,
                intent=message.intent,
                message_id=message.message_id,
                now_ms=now_ms,
                reply_to=_dispatch_reply_to(message),
            ):
                admitted.append(message)
        return tuple(admitted)
    def _forget_admission(self, message_id: str) -> None:
        """Drop in-memory held/admitted state once a row is no longer live."""

        forget = getattr(self._link_admission, "forget", None)
        if callable(forget):
            forget(message_id)

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
