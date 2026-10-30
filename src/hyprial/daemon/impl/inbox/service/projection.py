from __future__ import annotations
import json
from collections.abc import Callable
from typing import Self
from hyprial.daemon.impl.inbox.contracts.api  import (
    InboxMessage,
    InboxPruneItem,
    OutboxPruneItem,
)
from hyprial.daemon.impl.inbox.batch.contract import (
    SOURCE_INBOX,
    SOURCE_NOTICE,
    BatchBounds,
    BatchCursor,
    BatchMember,
    BatchRow,
    BatchSelection,
)
from hyprial.daemon.impl.inbox.contracts.summary import body_bytes, body_text
from hyprial.daemon.impl.inbox.history import (
    conversation_messages as _conversation_messages,
)
from hyprial.daemon.impl.inbox.links.pull  import (
    DeliveryStatus,
    DeliveryStatusStore,
)
from hyprial.daemon.impl.inbox.service.delivery.notices import _ExpiredSenderNotice
from hyprial.daemon.impl.inbox.service.synchronization import (
    _record_committed_change,
    _synchronized,
)

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
            removed = self._db.execute(
                """DELETE FROM inbox
                   WHERE consumed = 0
                     AND expires_at_ms IS NOT NULL
                     AND expires_at_ms <= ?""",
                (now_ms,),
            ).rowcount > 0
        _record_committed_change(self, removed)
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
    def conversation_messages(
        self,
        participant: str,
        conversation_id: str,
        *,
        after: tuple[int, str] | None = None,
        before: tuple[int, str] | None = None,
        limit: int = 50,
        newest_first: bool = False,
    ) -> tuple[InboxMessage, ...]:
        """Read-only page over retained inbox rows plus pending outbox rows."""

        return _conversation_messages(
            self._db,
            participant=participant,
            conversation_id=conversation_id,
            after=after,
            before=before,
            limit=limit,
            newest_first=newest_first,
        )
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
        self._stamp_inbox_fetch(messages, now, completed_turn=completed_turn)
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
        self._stamp_inbox_fetch(admitted, now, completed_turn=completed_turn)
        return admitted

    # -- inbox batch selection (read-only projection over live rows) --------

    def _batch_high_waters(self) -> tuple[int, int, int]:
        inbox_high = int(
            self._db.execute(
                "SELECT COALESCE(MAX(arrival_ordinal), 0) FROM inbox"
            ).fetchone()[0]
        )
        notice_high = int(
            self._db.execute(
                "SELECT COALESCE(MAX(arrival_ordinal), 0) FROM system_notices"
            ).fetchone()[0]
        )
        sequence = self._db.execute(
            "SELECT ordinal FROM inbox_arrival_sequence WHERE singleton = 1"
        ).fetchone()
        capture_ordinal = int(sequence[0])
        return inbox_high, notice_high, capture_ordinal

    @staticmethod
    def _batch_keys_sql(keys: tuple[str, ...]) -> str:
        return ", ".join("?" for _ in keys)

    def _batch_live_inbox(
        self, keys: tuple[str, ...], now: int, *, since: int | None = None
    ) -> tuple[InboxMessage, ...]:
        placeholders = self._batch_keys_sql(keys)
        since_sql = "" if since is None else "AND arrival_ordinal > ?"
        params: tuple[object, ...] = (*keys, now)
        if since is not None:
            params += (since,)
        rows = self._db.execute(
            f"""SELECT * FROM inbox WHERE recipient IN ({placeholders})
                  AND consumed = 0 AND (expires_at_ms IS NULL OR expires_at_ms > ?)
                  AND NOT EXISTS (SELECT 1 FROM harness_failure_settlements
                                   WHERE harness_failure_settlements.message_id = inbox.message_id
                                     AND harness_failure_settlements.terminal = 1)
                  {since_sql} ORDER BY arrival_ordinal""",  # noqa: S608
            params,
        ).fetchall()
        return tuple(self._row_message(row) for row in rows)

    def _batch_live_notices(
        self, keys: tuple[str, ...], *, since: int | None = None
    ) -> tuple[InboxMessage, ...]:
        placeholders = self._batch_keys_sql(keys)
        since_sql = "" if since is None else "AND arrival_ordinal > ?"
        params: tuple[object, ...] = (*keys,) if since is None else (*keys, since)
        rows = self._db.execute(
            f"SELECT * FROM system_notices WHERE recipient IN ({placeholders}) "  # noqa: S608
            f"{since_sql} ORDER BY arrival_ordinal",
            params,
        ).fetchall()
        return tuple(self._row_message(row) for row in rows)

    def _batch_ordinals(self, source: str, ids: tuple[str, ...]) -> dict[str, int]:
        if not ids:
            return {}
        table = "inbox" if source == SOURCE_INBOX else "system_notices"
        placeholders = ", ".join("?" for _ in ids)
        rows = self._db.execute(
            f"SELECT message_id, arrival_ordinal FROM {table} "  # noqa: S608
            f"WHERE message_id IN ({placeholders})",
            ids,
        ).fetchall()
        return {str(row[0]): int(row[1] or 0) for row in rows}

    @staticmethod
    def _batch_deferred_rows(
        source: str,
        messages: tuple[InboxMessage, ...],
        ordinals: dict[str, int],
        keys: set[tuple[str, str, int]],
    ) -> tuple[InboxMessage, ...]:
        return tuple(
            m for m in messages if (source, m.message_id, ordinals[m.message_id]) in keys
        )
    def _batch_row(self, source: str, message: InboxMessage, ordinal: int) -> BatchRow:
        return BatchRow(
            SOURCE_INBOX if source == SOURCE_INBOX else SOURCE_NOTICE,
            message,
            ordinal,
            body_text(message),
            body_bytes(message),
        )

    def _batch_choose(
        self,
        recipient: str,
        *,
        keys: tuple[str, ...],
        now: int,
        inbox_rows: tuple[InboxMessage, ...],
        notice_rows: tuple[InboxMessage, ...],
        bounds: BatchBounds,
        deferred: tuple[BatchMember, ...] = (),
        predecessor_digest: str | None = None,
        mark_fetched: bool = True,
    ) -> BatchSelection:
        """Admit inbox candidates once, then freeze members and deferred."""

        admitted = self._admit_dispatch(inbox_rows, now, frozenset())
        admitted_ids = {message.message_id for message in admitted}
        deleted = tuple(
            message for message in inbox_rows if message.message_id not in admitted_ids
        )
        inbox_ordinals = self._batch_ordinals(
            SOURCE_INBOX, tuple(message.message_id for message in inbox_rows)
        )
        notice_ordinals = self._batch_ordinals(
            SOURCE_NOTICE, tuple(message.message_id for message in notice_rows)
        )
        rows: list[BatchRow] = []
        seen: set[tuple[str, str]] = set()
        for source, messages, ordinals in (
            (SOURCE_INBOX, inbox_rows, inbox_ordinals),
            (SOURCE_NOTICE, notice_rows, notice_ordinals),
        ):
            for message in messages:
                key = (source, message.message_id)
                if key in seen or (
                    source == SOURCE_INBOX and message.message_id not in admitted_ids
                ):
                    continue
                seen.add(key)
                rows.append(
                    self._batch_row(source, message, ordinals[message.message_id])
                )
        still_deferred = tuple(
            BatchMember(SOURCE_INBOX, m.message_id, inbox_ordinals[m.message_id])
            for m in deleted
        )
        merged: list[BatchMember] = []
        seen_deferred: set[tuple[str, str, int]] = set()
        for member in (*deferred, *still_deferred):
            key = (member.source, member.message_id, member.ordinal)
            if key in seen_deferred:
                continue
            seen_deferred.add(key)
            merged.append(member)
        if mark_fetched:
            self._batch_stamp(admitted, notice_rows, now)
        return BatchSelection(
            recipient, bounds, tuple(rows), tuple(merged), predecessor_digest
        )

    def _stamp_inbox_fetch(
        self,
        inbox_rows: tuple[InboxMessage, ...],
        now: int,
        *,
        completed_turn: bool = False,
    ) -> None:
        """Commit actual fetch changes before publishing their external effects."""

        if inbox_rows:
            meta = self._inbound_ledger_meta(inbox_rows)
            with self._db:
                stamped = self._db.executemany(
                    """UPDATE inbox SET fetched_at_ms = ?
                        WHERE message_id = ? AND fetched_at_ms IS NULL""",
                    ((now, message.message_id) for message in inbox_rows),
                ).rowcount > 0
                fresh = [
                    message
                    for message in inbox_rows
                    if self._write_inbound_ledger_locked(
                        actor=message.recipient,
                        message=message,
                        received_at_ms=meta[message.message_id][0],
                        expires_at_ms=meta[message.message_id][1],
                    )
                ]
                cleared = self._clear_nonterminal_failures(inbox_rows, completed_turn)
            _record_committed_change(self, stamped or bool(fresh) or cleared)
            for message in fresh:
                self._sink_injected(
                    message.recipient, message, meta[message.message_id][0]
                )
            for message in inbox_rows:
                self.retire_outbox_receipt(
                    message.sender, message.message_id, now_ms=now
                )

    def _batch_stamp(
        self,
        inbox_rows: tuple[InboxMessage, ...],
        notice_rows: tuple[InboxMessage, ...],
        now: int,
    ) -> None:
        """One-way fetched stamp and its receipts; never an acknowledgement."""

        self._stamp_inbox_fetch(inbox_rows, now)
        if notice_rows:
            with self._db:
                stamped = self._db.executemany(
                    """UPDATE system_notices
                          SET fetched_at_ms = ?
                        WHERE message_id = ? AND fetched_at_ms IS NULL""",
                    ((now, message.message_id) for message in notice_rows),
                ).rowcount > 0
            _record_committed_change(self, stamped)

    @_synchronized
    def batch_snapshot(
        self,
        recipient: str,
        *,
        keys: tuple[str, ...],
        now_ms: int | None = None,
        cursor: BatchCursor | None = None,
        after: BatchCursor | None = None,
        after_digest: str | None = None,
        include_notices: bool = True,
        only: tuple[str, ...] | None = None,
        mark_fetched: bool = True,
    ) -> BatchSelection:
        """Select exactly one frozen batch; a read, never a settlement.

        A first capture freezes both high-waters and the shared ordinal, an
        exact member vector and the link-held identities.  A ``cursor`` replay
        reads only that vector and subtracts settled members.  An
        ``after`` successor admits the predecessor's still-deferred rows plus
        rows above its capture bound, and never repeats a predecessor member.
        """

        now = self._now_ms() if now_ms is None else now_ms
        allowed = None if only is None else frozenset(only)

        def keep(messages: tuple[InboxMessage, ...]) -> tuple[InboxMessage, ...]:
            if allowed is None:
                return messages
            return tuple(m for m in messages if m.message_id in allowed)

        if cursor is not None and after is not None:
            raise ValueError("cursor and afterCursor are mutually exclusive")
        inbox_high, notice_high, capture_ordinal = self._batch_high_waters()
        bounds = BatchBounds(inbox_high, notice_high, capture_ordinal)
        if cursor is not None:
            live_inbox = {
                i.message_id: i for i in keep(self._batch_live_inbox(keys, now))
            }
            live_notices = (
                {i.message_id: i for i in keep(self._batch_live_notices(keys))}
                if include_notices
                else {}
            )
            inbox_ordinals = self._batch_ordinals(
                SOURCE_INBOX,
                tuple(m.message_id for m in cursor.members if m.source == SOURCE_INBOX),
            )
            notice_ordinals = self._batch_ordinals(
                SOURCE_NOTICE,
                tuple(m.message_id for m in cursor.members if m.source == SOURCE_NOTICE),
            )
            rows: list[BatchRow] = []
            for member in cursor.members:
                if member.source == SOURCE_INBOX:
                    message = live_inbox.get(member.message_id)
                    live_ordinal = inbox_ordinals.get(member.message_id)
                else:
                    message = live_notices.get(member.message_id)
                    live_ordinal = notice_ordinals.get(member.message_id)
                if message is None or live_ordinal != member.ordinal:
                    continue
                rows.append(self._batch_row(member.source, message, member.ordinal))
            admitted = tuple(r.message for r in rows if r.source == SOURCE_INBOX)
            noticed = tuple(r.message for r in rows if r.source == SOURCE_NOTICE)
            if mark_fetched:
                self._batch_stamp(admitted, noticed, now)
            return BatchSelection(
                recipient, cursor.bounds, tuple(rows), cursor.deferred,
                cursor.predecessor_digest,
            )
        if after is None:
            return self._batch_choose(
                recipient,
                keys=keys,
                now=now,
                inbox_rows=keep(self._batch_live_inbox(keys, now)),
                notice_rows=(
                    keep(self._batch_live_notices(keys)) if include_notices else ()
                ),
                bounds=bounds,
                mark_fetched=mark_fetched,
            )
        deferred_keys = {
            (m.source, m.message_id, m.ordinal) for m in after.deferred
        }
        new_inbox = keep(
            self._batch_live_inbox(keys, now, since=after.bounds.capture_ordinal)
        )
        new_notices = (
            keep(self._batch_live_notices(keys, since=after.bounds.capture_ordinal))
            if include_notices
            else ()
        )
        live_inbox = keep(self._batch_live_inbox(keys, now))
        live_notices = keep(self._batch_live_notices(keys)) if include_notices else ()
        inbox_ordinals = self._batch_ordinals(
            SOURCE_INBOX, tuple(message.message_id for message in live_inbox)
        )
        notice_ordinals = self._batch_ordinals(
            SOURCE_NOTICE, tuple(message.message_id for message in live_notices)
        )
        deferred_inbox = self._batch_deferred_rows(
            SOURCE_INBOX, live_inbox, inbox_ordinals, deferred_keys
        )
        deferred_notices = self._batch_deferred_rows(
            SOURCE_NOTICE, live_notices, notice_ordinals, deferred_keys
        )
        inbox_candidates = tuple(
            {m.message_id: m for m in (*deferred_inbox, *new_inbox)}.values())
        notice_candidates = tuple(
            {m.message_id: m for m in (*deferred_notices, *new_notices)}.values())
        return self._batch_choose(
            recipient,
            keys=keys,
            now=now,
            inbox_rows=inbox_candidates,
            notice_rows=notice_candidates,
            bounds=bounds,
            predecessor_digest=after_digest,
            mark_fetched=mark_fetched,
        )

    def _clear_nonterminal_failures(
        self, messages: tuple[InboxMessage, ...], completed_turn: bool
    ) -> bool:
        """Drop retry eligibility for a successfully completed turn only.

        The append-only attempt log and any terminal tombstone stay; a FAILED
        turn never calls this.
        """

        if not completed_turn:
            return False
        return self._db.executemany(
            """DELETE FROM harness_failure_settlements
                WHERE message_id = ? AND terminal = 0""",
            ((message.message_id,) for message in messages),
        ).rowcount > 0

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
