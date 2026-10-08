"""Crash-safe persistent Lark message correlation state, backed by SQLite.

One shared database (``~/.hyprial/state/adapters.sqlite3``) serves every adapter
process on the host; rows are namespaced by an ``adapter`` column.  The
storage conventions follow ``hyprial.inbox.service``: WAL journal, full
synchronous, and a busy timeout so concurrent adapter processes queue on the
write lock instead of failing.  Mutations run under ``BEGIN IMMEDIATE`` so a
read-modify-write (capacity checks, upsert-detection) is atomic across
processes, not just across threads.

There is no migration from the JSON era (product decision: the old runtime
state is void after the cutover).  A pre-SQLite state file found on disk is
renamed with a ``.retired`` suffix -- kept for audit, never read -- and the
store starts empty.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from hyprial.daemon.impl.adapters.lark.contracts.wire import PendingCommandCapacityStatus
from hyprial.daemon.impl.adapters.lark.state.readers import retire_legacy_state
from hyprial.daemon.impl.adapters.lark.state.records import (
    DEAD_LETTER_DETAIL_LIMIT,
    DEAD_LETTER_TEXT_LIMIT,
    DEFAULT_ADAPTER,
    DeadLetter,
    Identity,
    MAX_CORRELATIONS,
    MAX_DEAD_LETTERS,
    MAX_PENDING_INBOUND_SUBMISSIONS,
    MAX_PENDING_INBOUND_SUBMISSION_BYTES,
    PendingCommandCapacityError,
    PendingCommandResponse,
    PendingInboundSubmissionCapacityError,
    ReplyRoute,
    RequestCorrelation,
    _BUSY_TIMEOUT_MS,
    _DEAD_LETTER_COLUMNS,
    _IDENTITY_COLUMNS,
    _PENDING_COLUMNS,
    _checked_identity,
    _escape_like,
    _pending_capacity,
    _pending_responses,
)
from hyprial.daemon.impl.adapters.lark.state.schema import _LarkStateSchema
class LarkStateStore(_LarkStateSchema):
    def __init__(
        self,
        path: Path,
        *,
        adapter: str = DEFAULT_ADAPTER,
        legacy_path: Path | None = None,
    ) -> None:
        self.path = path
        self.adapter = adapter
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        if legacy_path is not None and legacy_path.resolve() != path.resolve():
            retire_legacy_state(legacy_path)
        self._retire_foreign_database()
        # Explicit transaction control (``isolation_level=None``): Python's
        # implicit mode would start the write transaction only at the first
        # DML statement, leaving the reads of a read-modify-write outside it.
        self._db = sqlite3.connect(
            path,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            self._initialize()
        except BaseException:
            # Constructor failures retain self via their traceback. Closing
            # here rolls back any unfinished schema transaction and avoids
            # pinning WAL handles until the exception is eventually collected.
            self._db.close()
            raise


    def request(self, harness_message_id: str) -> RequestCorrelation | None:
        with self._lock:
            row = self._db.execute(
                "SELECT harness_message_id, message_id, chat_id, conversation_id"
                " FROM request_correlations"
                " WHERE adapter = ? AND harness_message_id = ?",
                (self.adapter, harness_message_id),
            ).fetchone()
        return None if row is None else RequestCorrelation(**dict(row))

    def reply(self, native_message_id: str) -> ReplyRoute | None:
        with self._lock:
            row = self._db.execute(
                "SELECT message_id, actor_id, actor_key, harness_message_id,"
                " conversation_id, chat_id FROM reply_routes"
                " WHERE adapter = ? AND message_id = ?",
                (self.adapter, native_message_id),
            ).fetchone()
        return None if row is None else ReplyRoute(**dict(row))

    def seen(self, event_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM seen_events WHERE adapter = ? AND event_id = ?",
                (self.adapter, event_id),
            ).fetchone()
        return row is not None

    def seen_message(self, message_id: str) -> bool:
        """Whether this native message id already reached Harness custody.

        Unlike ``seen`` (keyed by event id), this deduplicates across
        delivery paths: a live websocket event, a manual ``recover_message``
        replay, and a reconnect reconciliation can all surface the same
        native message and only the first may be forwarded.
        """

        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM seen_messages"
                " WHERE adapter = ? AND message_id = ?",
                (self.adapter, message_id),
            ).fetchone()
        return row is not None

    def chat_type(self, chat_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT chat_type FROM chat_types"
                " WHERE adapter = ? AND chat_id = ?",
                (self.adapter, chat_id),
            ).fetchone()
        if row is None:
            return None
        value = row["chat_type"]
        return value if isinstance(value, str) and value else None

    def record_chat_type(self, chat_id: str, chat_type: str) -> None:
        """Remember a chat's type as observed on live events.

        The REST message models carry no ``chat_type``, so recovery paths
        reuse the type observed when the chat last produced a live event.
        """

        with self._transaction() as db:
            db.execute(
                """INSERT INTO chat_types(adapter, chat_id, chat_type)
                   VALUES (?, ?, ?)
                   ON CONFLICT(adapter, chat_id)
                   DO UPDATE SET chat_type = excluded.chat_type""",
                (self.adapter, chat_id, chat_type),
            )
            self._prune(db, "chat_types", MAX_CORRELATIONS)

    def recent_chats(self) -> tuple[str, ...]:
        """Chats with prior inbound activity — the reconciliation scan set.

        Retired chats (permanently refused by the platform) are excluded;
        their rows in the contributing tables are kept as audit records.
        """

        with self._lock:
            rows = self._db.execute(
                """SELECT chat_id FROM request_correlations WHERE adapter = ?
                   UNION
                   SELECT chat_id FROM dead_letters WHERE adapter = ?
                   EXCEPT
                   SELECT chat_id FROM retired_chats WHERE adapter = ?
                   ORDER BY chat_id""",
                (self.adapter, self.adapter, self.adapter),
            ).fetchall()
        return tuple(row["chat_id"] for row in rows)

    def retire_chat(self, chat_id: str, *, code: int, now: datetime) -> bool:
        """Mark a chat permanently unavailable; report if newly retired.

        Idempotent: re-retiring the same chat keeps the first observation.
        """

        with self._transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO retired_chats(
                       adapter, chat_id, code, retired_at
                   ) VALUES (?, ?, ?, ?)""",
                (self.adapter, chat_id, code, now.isoformat()),
            )
            return cursor.rowcount > 0

    def retired_chat_code(self, chat_id: str) -> int | None:
        """The platform code a chat was retired with, or None if active."""

        with self._lock:
            row = self._db.execute(
                "SELECT code FROM retired_chats"
                " WHERE adapter = ? AND chat_id = ?",
                (self.adapter, chat_id),
            ).fetchone()
        return None if row is None else int(row["code"])

    def unretire_chat(self, chat_id: str) -> bool:
        """Lift a chat's retirement; report whether one was lifted.

        Live inbound traffic from the chat is direct evidence the bot is a
        member again (e.g. re-added after removal), so the chat rejoins the
        reconciliation scan set.  Retirement is only ever lifted by this
        out-of-band fact, never by the sweep that imposed it.
        """

        with self._transaction() as db:
            cursor = db.execute(
                "DELETE FROM retired_chats WHERE adapter = ? AND chat_id = ?",
                (self.adapter, chat_id),
            )
            return cursor.rowcount > 0

    def record_dead_letter(self, letter: DeadLetter) -> bool:
        """Persist an audit record and report whether it was newly discovered."""

        # Truncation lives in the store so no caller can bypass it.
        bounded_letter = replace(
            letter,
            text=letter.text[:DEAD_LETTER_TEXT_LIMIT],
            detail=(
                letter.detail[:DEAD_LETTER_DETAIL_LIMIT]
                if letter.detail is not None
                else None
            ),
        )
        with self._transaction() as db:
            # Upsert keyed by native message id: re-processing the same lost
            # message refreshes the record instead of growing the trail.
            created = (
                db.execute(
                    "SELECT 1 FROM dead_letters"
                    " WHERE adapter = ? AND message_id = ?",
                    (self.adapter, bounded_letter.message_id),
                ).fetchone()
                is None
            )
            db.execute(
                """INSERT INTO dead_letters(
                       adapter, message_id, event_id, chat_id, chat_type,
                       conversation_id, sender_id, sender_type, text, reason,
                       created_at, detail, create_time, reply_to, message_type
                   ) VALUES (
                       :adapter, :message_id, :event_id, :chat_id, :chat_type,
                       :conversation_id, :sender_id, :sender_type, :text,
                       :reason, :created_at, :detail, :create_time, :reply_to,
                       :message_type
                   )
                   ON CONFLICT(adapter, message_id) DO UPDATE SET
                       event_id = excluded.event_id,
                       chat_id = excluded.chat_id,
                       chat_type = excluded.chat_type,
                       conversation_id = excluded.conversation_id,
                       sender_id = excluded.sender_id,
                       sender_type = excluded.sender_type,
                       text = excluded.text,
                       reason = excluded.reason,
                       created_at = excluded.created_at,
                       detail = excluded.detail,
                       create_time = excluded.create_time,
                       reply_to = excluded.reply_to,
                       message_type = excluded.message_type""",
                {**asdict(bounded_letter), "adapter": self.adapter},
            )
            self._prune(db, "dead_letters", MAX_DEAD_LETTERS)
            return created

    def dead_letters(
        self,
        *,
        reason: str | None = None,
        chat_id: str | None = None,
        since: str | None = None,
    ) -> tuple[DeadLetter, ...]:
        """The audit trail, optionally filtered on its indexed columns.

        ``since`` compares ``created_at`` lexicographically, which is
        chronological for the ISO-8601 UTC timestamps the adapter writes.
        """

        query = [
            f"SELECT {_DEAD_LETTER_COLUMNS} FROM dead_letters WHERE adapter = ?"
        ]
        params: list[str] = [self.adapter]
        if reason is not None:
            query.append("AND reason = ?")
            params.append(reason)
        if chat_id is not None:
            query.append("AND chat_id = ?")
            params.append(chat_id)
        if since is not None:
            query.append("AND created_at >= ?")
            params.append(since)
        query.append("ORDER BY rowid")
        with self._lock:
            rows = self._db.execute(" ".join(query), params).fetchall()
        return tuple(DeadLetter(**dict(row)) for row in rows)

    def dead_letter_summary(self, *, since: str) -> tuple[int, dict[str, int]]:
        """Return a body-free recent count grouped by machine reason."""

        with self._lock:
            rows = self._db.execute(
                """SELECT reason, COUNT(*) AS count
                   FROM dead_letters
                   WHERE adapter = ? AND created_at >= ?
                   GROUP BY reason
                   ORDER BY reason""",
                (self.adapter, since),
            ).fetchall()
        reasons = {str(row["reason"]): int(row["count"]) for row in rows}
        return sum(reasons.values()), reasons

    def claim_dead_letter_alert(self, *, window_start: str, claimed_at: str) -> bool:
        """Atomically claim one adapter/window notification across processes."""

        with self._transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO dead_letter_alerts(
                       adapter, window_start, claimed_at
                   ) VALUES (?, ?, ?)""",
                (self.adapter, window_start, claimed_at),
            )
            # The table is a dedupe ledger, not an audit trail.  Keep a bounded
            # number of newest claims per adapter without wall-clock guesses.
            db.execute(
                """DELETE FROM dead_letter_alerts
                   WHERE adapter = ? AND rowid NOT IN (
                       SELECT rowid FROM dead_letter_alerts
                       WHERE adapter = ?
                       ORDER BY window_start DESC
                       LIMIT 48
                   )""",
                (self.adapter, self.adapter),
            )
            return cursor.rowcount == 1

    def release_dead_letter_alert(self, *, window_start: str) -> None:
        """Release an unsent claim so a later write may retry the notification."""

        with self._transaction() as db:
            db.execute(
                """DELETE FROM dead_letter_alerts
                   WHERE adapter = ? AND window_start = ?""",
                (self.adapter, window_start),
            )

    def claim_alarm(
        self,
        conversation_id: str,
        reason: str,
        window_start_ms: int,
        claimed_at_ms: int,
    ) -> bool:
        """Atomically claim one conversation/reason alarm window."""

        with self._transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO alarm_throttle(
                       adapter, conversation_id, reason, window_start_ms,
                       claimed_at_ms
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    self.adapter,
                    conversation_id,
                    reason,
                    window_start_ms,
                    claimed_at_ms,
                ),
            )
            db.execute(
                """DELETE FROM alarm_throttle
                   WHERE adapter = ? AND window_start_ms < ?""",
                (self.adapter, window_start_ms - 3_600_000),
            )
            return cursor.rowcount == 1

    def clear_dead_letter(self, message_id: str) -> None:
        with self._transaction() as db:
            self._clear_dead_letter(db, message_id)

    def pending_inbound_submission(self, message_id: str) -> str | None:
        record = self.pending_inbound_submission_record(message_id)
        return None if record is None else record[2]

    def pending_inbound_submission_record(self, message_id: str) -> tuple[str, str, str] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT message_id, event_id, request FROM pending_inbound_submissions"
                " WHERE adapter = ? AND message_id = ?",
                (self.adapter, message_id),
            ).fetchone()
        return None if row is None else tuple(str(row[key]) for key in ("message_id", "event_id", "request"))

    def pending_inbound_submission_page(self, *, limit: int) -> tuple[tuple[str, str, str], ...]:
        """Bounded circular keyset scan; its durable cursor survives restarts."""
        if not 1 <= limit <= MAX_PENDING_INBOUND_SUBMISSIONS:
            raise ValueError("invalid pending submission page limit")
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (f"pending-inbound-cursor:{self.adapter}",)).fetchone()
            after = None if row is None else str(row["value"])
            rows = self._db.execute(
                "SELECT message_id, event_id, request FROM pending_inbound_submissions"
                " WHERE adapter = ? AND (? IS NULL OR message_id > ?) ORDER BY message_id LIMIT ?",
                (self.adapter, after, after, limit),
            ).fetchall()
            if after is not None and len(rows) < limit:
                rows += self._db.execute(
                    "SELECT message_id, event_id, request FROM pending_inbound_submissions"
                    " WHERE adapter = ? AND message_id <= ? ORDER BY message_id LIMIT ?",
                    (self.adapter, after, limit - len(rows)),
                ).fetchall()
        return tuple(tuple(str(row[key]) for key in ("message_id", "event_id", "request")) for row in rows)

    def advance_pending_inbound_cursor(self, message_id: str) -> None:
        with self._transaction() as db:
            db.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (f"pending-inbound-cursor:{self.adapter}", message_id))

    def pending_inbound_submission_count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM pending_inbound_submissions WHERE adapter = ?", (self.adapter,)).fetchone()[0])

    def record_pending_inbound_submission(
        self, message_id: str, event_id: str, request: str
    ) -> tuple[str, bool]:
        """Freeze once under an atomic count/byte budget; never evict custody."""
        if not all(isinstance(value, str) and value for value in (message_id, event_id, request)):
            raise ValueError("pending inbound submission fields must be non-empty strings")
        size = sum(len(value.encode("utf-8")) for value in (message_id, event_id, request))
        with self._transaction() as db:
            current = db.execute(
                "SELECT request FROM pending_inbound_submissions"
                " WHERE adapter = ? AND message_id = ?",
                (self.adapter, message_id),
            ).fetchone()
            if current is not None:
                return str(current["request"]), False
            count, stored_bytes = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(CAST(message_id AS BLOB))"
                " + length(CAST(event_id AS BLOB)) + length(CAST(request AS BLOB))), 0)"
                " FROM pending_inbound_submissions WHERE adapter = ?",
                (self.adapter,),
            ).fetchone()
            if count >= MAX_PENDING_INBOUND_SUBMISSIONS or stored_bytes + size > MAX_PENDING_INBOUND_SUBMISSION_BYTES:
                raise PendingInboundSubmissionCapacityError("pending inbound submission capacity exhausted")
            db.execute(
                "INSERT INTO pending_inbound_submissions(adapter, message_id, event_id, request, notice_claimed)"
                " VALUES (?, ?, ?, ?, 0)", (self.adapter, message_id, event_id, request),
            )
        return request, True

    def claim_pending_inbound_notice(self, message_id: str) -> bool:
        """Commit one notification-attempt claim before any native side effect.

        Never release it after failure, throttling or an interrupted attempt.
        Successful submission settlement deletes it with the pending row.
        """
        with self._transaction() as db:
            cursor = db.execute(
                "UPDATE pending_inbound_submissions SET notice_claimed = 1"
                " WHERE adapter = ? AND message_id = ? AND notice_claimed = 0",
                (self.adapter, message_id),
            )
            return cursor.rowcount == 1

    def discard_pending_inbound_submission(self, message_id: str, request: str) -> None:
        """Release only the exact frozen request after a definitive refusal."""
        with self._transaction() as db:
            db.execute(
                "DELETE FROM pending_inbound_submissions"
                " WHERE adapter = ? AND message_id = ? AND request = ?",
                (self.adapter, message_id, request),
            )

    def record_inbound(
        self,
        correlation: RequestCorrelation,
        *,
        event_id: str,
    ) -> None:
        with self._transaction() as db:
            db.execute(
                """INSERT INTO request_correlations(
                       adapter, harness_message_id, message_id, chat_id,
                       conversation_id
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(adapter, harness_message_id) DO UPDATE SET
                       message_id = excluded.message_id,
                       chat_id = excluded.chat_id,
                       conversation_id = excluded.conversation_id""",
                (
                    self.adapter,
                    correlation.harness_message_id,
                    correlation.message_id,
                    correlation.chat_id,
                    correlation.conversation_id,
                ),
            )
            self._prune(db, "request_correlations", MAX_CORRELATIONS)
            self._mark_seen_event(db, event_id)
            # The native id is the cross-path idempotency key: once Harness
            # accepted custody, no replay of this message may re-forward.
            self._mark_seen_message(db, correlation.message_id)
            # A successful forward resolves any earlier dead letter for the
            # same native message.
            self._clear_dead_letter(db, correlation.message_id)
            db.execute(
                "DELETE FROM pending_inbound_submissions WHERE adapter = ? AND message_id = ?",
                (self.adapter, correlation.message_id),
            )

    def record_reply(self, route: ReplyRoute) -> None:
        with self._transaction() as db:
            db.execute(
                """INSERT INTO reply_routes(
                       adapter, message_id, actor_id, actor_key,
                       harness_message_id, conversation_id, chat_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(adapter, message_id) DO UPDATE SET
                       actor_id = excluded.actor_id,
                       actor_key = excluded.actor_key,
                       harness_message_id = excluded.harness_message_id,
                       conversation_id = excluded.conversation_id,
                       chat_id = excluded.chat_id""",
                (
                    self.adapter,
                    route.message_id,
                    route.actor_id,
                    route.actor_key,
                    route.harness_message_id,
                    route.conversation_id,
                    route.chat_id,
                ),
            )
            self._prune(db, "reply_routes", MAX_CORRELATIONS)

    def record_seen(self, event_id: str) -> None:
        with self._transaction() as db:
            self._mark_seen_event(db, event_id)

    def record_consumed(self, event_id: str, message_id: str) -> None:
        """Mark a Lark-local command consumed across live and replay paths."""

        with self._transaction() as db:
            self._mark_seen_event(db, event_id)
            self._mark_seen_message(db, message_id)
            self._clear_dead_letter(db, message_id)

    def _pending_rows(self, db: sqlite3.Connection) -> dict[str, Any]:
        rows = db.execute(
            f"SELECT {_PENDING_COLUMNS} FROM pending_command_responses"
            " WHERE adapter = ? ORDER BY rowid",
            (self.adapter,),
        ).fetchall()
        return {row["message_id"]: dict(row) for row in rows}

    def pending_command_response(
        self, message_id: str
    ) -> PendingCommandResponse | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {_PENDING_COLUMNS} FROM pending_command_responses"
                " WHERE adapter = ? AND message_id = ?",
                (self.adapter, message_id),
            ).fetchone()
        if row is None:
            return None
        # Route the row through the shared parser so corrupt persisted
        # material fails closed exactly like it did at JSON load time.
        return _pending_responses({message_id: dict(row)})[message_id]

    def pending_command_capacity(self) -> PendingCommandCapacityStatus:
        with self._lock:
            return _pending_capacity(self._pending_rows(self._db))

    def record_pending_command_response(
        self, response: PendingCommandResponse
    ) -> PendingCommandResponse:
        """Persist once and return the immutable response owned by message id."""

        with self._transaction() as db:
            current = self._pending_rows(db)
            raw = current.get(response.message_id)
            if raw is not None:
                return PendingCommandResponse(**raw)
            # Validate and measure the candidate map before writing.  This
            # runs inside ``BEGIN IMMEDIATE``, so concurrent writers cannot
            # both observe the last free slot/byte budget.
            candidate = dict(current)
            candidate[response.message_id] = asdict(response)
            capacity = _pending_capacity(candidate)
            if (
                capacity.count > capacity.max_count
                or capacity.bytes > capacity.max_bytes
            ):
                raise PendingCommandCapacityError(
                    replace(_pending_capacity(current), status="full")
                )
            db.execute(
                "INSERT INTO pending_command_responses(adapter, message_id,"
                " event_id, kind, text, idempotency_key)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.adapter,
                    response.message_id,
                    response.event_id,
                    response.kind,
                    response.text,
                    response.idempotency_key,
                ),
            )
            return response

    def complete_command_response(
        self,
        response: PendingCommandResponse,
        *,
        delivered_event_id: str,
    ) -> None:
        """Atomically retire pending response and consume every delivery id."""

        with self._transaction() as db:
            row = db.execute(
                f"SELECT {_PENDING_COLUMNS} FROM pending_command_responses"
                " WHERE adapter = ? AND message_id = ?",
                (self.adapter, response.message_id),
            ).fetchone()
            if row is None or PendingCommandResponse(**dict(row)) != response:
                raise ValueError(
                    "pending Lark command response changed before completion"
                )
            db.execute(
                "DELETE FROM pending_command_responses"
                " WHERE adapter = ? AND message_id = ?",
                (self.adapter, response.message_id),
            )
            self._mark_seen_event(db, response.event_id)
            self._mark_seen_event(db, delivered_event_id)
            self._mark_seen_message(db, response.message_id)
            self._clear_dead_letter(db, response.message_id)

    def conversation_timezone(self, conversation_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT timezone FROM conversation_timezones"
                " WHERE adapter = ? AND conversation_id = ?",
                (self.adapter, conversation_id),
            ).fetchone()
        if row is None:
            return None
        value = row["timezone"]
        return value if isinstance(value, str) and value else None

    def set_conversation_timezone(
        self, conversation_id: str, timezone: str | None
    ) -> None:
        with self._transaction() as db:
            if timezone is None:
                db.execute(
                    "DELETE FROM conversation_timezones"
                    " WHERE adapter = ? AND conversation_id = ?",
                    (self.adapter, conversation_id),
                )
            else:
                db.execute(
                    """INSERT INTO conversation_timezones(
                           adapter, conversation_id, timezone
                       ) VALUES (?, ?, ?)
                       ON CONFLICT(adapter, conversation_id)
                       DO UPDATE SET timezone = excluded.timezone""",
                    (self.adapter, conversation_id, timezone),
                )
                self._prune(db, "conversation_timezones", MAX_CORRELATIONS)

    def observe_identity(
        self,
        kind: str,
        platform_id: str,
        *,
        display_name: str | None = None,
        union_id: str | None = None,
        source: str,
        now_ms: int | None = None,
    ) -> None:
        """Record a mechanically observed identity, idempotently.

        A repeat observation refreshes ``last_seen_ms`` (and the display name
        and union_id when the observation carries them) but never moves
        ``first_seen_ms``.  A ``verified`` row keeps its standing, owner and
        source: observations may only add platform facts, never overwrite a
        human confirmation.
        """

        _checked_identity(
            Identity(kind=kind, platform_id=platform_id, source=source)
        )
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        with self._transaction() as db:
            db.execute(
                """INSERT INTO identities(
                       adapter, kind, platform_id, display_name, union_id,
                       hyprial_owner, standing, source, first_seen_ms,
                       last_seen_ms
                   ) VALUES (?, ?, ?, ?, ?, NULL, 'observed', ?, ?, ?)
                   ON CONFLICT(adapter, kind, platform_id) DO UPDATE SET
                       display_name = COALESCE(
                           NULLIF(excluded.display_name, ''),
                           identities.display_name
                       ),
                       union_id = COALESCE(
                           NULLIF(excluded.union_id, ''),
                           identities.union_id
                       ),
                       source = CASE
                           WHEN identities.standing = 'verified'
                           THEN identities.source
                           ELSE excluded.source
                       END,
                       first_seen_ms = COALESCE(
                           identities.first_seen_ms, excluded.first_seen_ms
                       ),
                       last_seen_ms = excluded.last_seen_ms""",
                (
                    self.adapter,
                    kind,
                    platform_id,
                    display_name,
                    union_id,
                    source,
                    now_ms,
                    now_ms,
                ),
            )

    def upsert_identity(
        self, identity: Identity, *, now_ms: int | None = None
    ) -> Identity:
        """Authoritatively write one identity mapping (operator surface).

        Unlike :meth:`observe_identity` this overwrites display name, owner,
        standing and source -- it is the only path that may set or revoke
        ``verified``.  ``first_seen_ms`` is preserved for an existing row
        unless the caller supplies one explicitly.
        """

        _checked_identity(identity)
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        first_seen = (
            identity.first_seen_ms
            if identity.first_seen_ms is not None
            else now_ms
        )
        last_seen = (
            identity.last_seen_ms
            if identity.last_seen_ms is not None
            else now_ms
        )
        explicit_first_seen = identity.first_seen_ms is not None
        with self._transaction() as db:
            db.execute(
                """INSERT INTO identities(
                       adapter, kind, platform_id, display_name, union_id,
                       hyprial_owner, standing, source, first_seen_ms,
                       last_seen_ms
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(adapter, kind, platform_id) DO UPDATE SET
                       display_name = excluded.display_name,
                       union_id = COALESCE(
                           excluded.union_id, identities.union_id
                       ),
                       hyprial_owner = excluded.hyprial_owner,
                       standing = excluded.standing,
                       source = excluded.source,
                       first_seen_ms = CASE
                           WHEN ? THEN excluded.first_seen_ms
                           ELSE COALESCE(
                               identities.first_seen_ms, excluded.first_seen_ms
                           )
                       END,
                       last_seen_ms = excluded.last_seen_ms""",
                (
                    self.adapter,
                    identity.kind,
                    identity.platform_id,
                    identity.display_name,
                    identity.union_id,
                    identity.hyprial_owner,
                    identity.standing,
                    identity.source,
                    first_seen,
                    last_seen,
                    explicit_first_seen,
                ),
            )
            row = db.execute(
                f"SELECT {_IDENTITY_COLUMNS} FROM identities"
                " WHERE adapter = ? AND kind = ? AND platform_id = ?",
                (self.adapter, identity.kind, identity.platform_id),
            ).fetchone()
        return Identity(**dict(row))

    def identity(self, kind: str, platform_id: str) -> Identity | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {_IDENTITY_COLUMNS} FROM identities"
                " WHERE adapter = ? AND kind = ? AND platform_id = ?",
                (self.adapter, kind, platform_id),
            ).fetchone()
        return None if row is None else Identity(**dict(row))

    def identities(self, *, kind: str | None = None) -> tuple[Identity, ...]:
        query = [
            f"SELECT {_IDENTITY_COLUMNS} FROM identities WHERE adapter = ?"
        ]
        params: list[str] = [self.adapter]
        if kind is not None:
            query.append("AND kind = ?")
            params.append(kind)
        query.append("ORDER BY kind, platform_id")
        with self._lock:
            rows = self._db.execute(" ".join(query), params).fetchall()
        return tuple(Identity(**dict(row)) for row in rows)

    def identities_by_union_id(
        self, union_id: str, *, kind: str = "user"
    ) -> tuple[tuple[str, Identity], ...]:
        """Every adapter's row for one cross-App identity, as (adapter, row).

        Deliberately NOT scoped to this adapter: ``union_id`` is the only join
        between App namespaces (open_ids differ per App), so a human verified
        once -- under whichever adapter a person confirmed them -- is known to
        every adapter that has seen the same union_id.  Read-only.
        """

        if not union_id:
            return ()
        with self._lock:
            rows = self._db.execute(
                f"SELECT adapter, {_IDENTITY_COLUMNS} FROM identities"
                " WHERE union_id = ? AND kind = ?"
                " ORDER BY adapter, platform_id",
                (union_id, kind),
            ).fetchall()
        return tuple(
            (
                row["adapter"],
                Identity(**{k: row[k] for k in row.keys() if k != "adapter"}),
            )
            for row in rows
        )

    def find_identities(
        self,
        *,
        name: str | None = None,
        platform_id: str | None = None,
    ) -> tuple[Identity, ...]:
        """Search by display-name substring and/or exact platform id."""

        query = [
            f"SELECT {_IDENTITY_COLUMNS} FROM identities WHERE adapter = ?"
        ]
        params: list[str] = [self.adapter]
        if name is not None:
            query.append(r"AND display_name LIKE ? ESCAPE '\'")
            params.append(f"%{_escape_like(name)}%")
        if platform_id is not None:
            query.append("AND platform_id = ?")
            params.append(platform_id)
        query.append("ORDER BY kind, platform_id")
        with self._lock:
            rows = self._db.execute(" ".join(query), params).fetchall()
        return tuple(Identity(**dict(row)) for row in rows)

    def close(self) -> None:
        with self._lock:
            self._db.close()
