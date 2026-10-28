"""Durable send-link bookkeeping inside the inbox SQLite authority (WP2 §3.9).

One table of ledger rows and one of task bindings live beside ``outbox`` so
every open request, admission, review, drain and PAC binding commits in the
same transaction as the message state change it describes.  The links engine
never reads this module: composition adapts ``LinkInboxPort`` onto the facade,
and the inbox only tells the engine what happened through ``LinkLedgerSink``.

Everything here runs on the serialized inbox mutation context (the actor
thread, or one RLock for a raw service), so the callbacks below fire after the
transaction commits and change memory only.
"""

from __future__ import annotations

import json
from dataclasses import fields
from typing import Any

from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.inbox.contracts.api import (
    InboxMessage,
    LinkLedgerOpen,
    LinkLedgerResult,
)
from hyprial.daemon.impl.inbox.contracts.ports import (
    LinkLedgerSink,
    UpdateLinkLedgerCommand,
)
from hyprial.daemon.impl.inbox.service.synchronization import (
    _record_committed_change,
    _synchronized,
)

#: Durable states a row never leaves (§3.9).  Purge only touches these.
_TERMINAL_STATES = ("closed", "acked", "expired", "drained", "released")
#: Inbound rows a reply may still close.
_LIVE_INBOUND_STATES = ("open", "censored")
_UPDATE_OPERANDS = frozenset({
    "release", "reasons", "fail", "drain", "cancel_outbound", "release_outbound",
    "censor", "sampled", "open_outbound", "close_inbound", "bind_tasks", "unbind_tasks",
})


def _retention_only_update(command: UpdateLinkLedgerCommand) -> bool:
    """Recognize the audited maintenance shape; new operands stay conservative."""

    operands = {item.name for item in fields(command)} - {
        "correlation_id", "now_ms", "purge_before_ms",
    }
    return operands == _UPDATE_OPERANDS and not any(
        getattr(command, name) for name in operands
    )


def _link_reply_to(message: InboxMessage) -> str | None:
    """The reply association of a message body (review B1.2), or None."""

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


def _outbox_reply_target(row: Any) -> str | None:
    """The reply association one outbox row carries, or None.

    The body's ``replyTo`` wins (ruling 55 writes it on every agent
    reply); rows from before that change still answer through the
    ``reply:`` idempotency prefix.  The association comes from the outbox
    row itself because a held reply books no ledger ``out`` row -- the
    seam wp2b-acceptance-1's rework 1 closes.
    """

    try:
        body = json.loads(row["payload"])
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
        body = None
    if isinstance(body, dict):
        reply_to = body.get("replyTo")
        if isinstance(reply_to, str) and reply_to:
            return reply_to
    key = row["idempotency_key"]
    if isinstance(key, str) and key.startswith("reply:"):
        original = key[len("reply:") :]
        if original:
            return original
    return None


class InboxServiceLinkLedgerMixin:
    """Ledger reads, writes and the post-commit sink callbacks."""

    # -- schema ---------------------------------------------------------

    def _create_link_ledger_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS link_ledger (
              actor TEXT NOT NULL,
              message_id TEXT NOT NULL,
              direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
              peer TEXT NOT NULL,
              intent TEXT NOT NULL,
              reply_to TEXT,
              state TEXT NOT NULL,
              opened_at_ms INTEGER NOT NULL,
              sent_at_ms INTEGER,
              event_at_ms INTEGER,
              threshold_ms INTEGER,
              sample_pending INTEGER NOT NULL DEFAULT 0,
              expires_at_ms INTEGER NOT NULL,
              PRIMARY KEY (actor, message_id)
            );
            CREATE INDEX IF NOT EXISTS link_ledger_live
                ON link_ledger(direction, state, actor);
            CREATE TABLE IF NOT EXISTS link_task_bindings (
              actor TEXT NOT NULL,
              peer TEXT NOT NULL,
              task_id TEXT NOT NULL,
              bound_at_ms INTEGER NOT NULL,
              PRIMARY KEY (actor, peer, task_id)
            );
            """
        )
        outbox_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(outbox)").fetchall()
        }
        if "link_hold" not in outbox_columns:
            self._db.execute("ALTER TABLE outbox ADD COLUMN link_hold TEXT")

    # -- sink resolution ------------------------------------------------

    def _link_ledger_sink(self) -> LinkLedgerSink | None:
        """The bound gate when it also speaks ``LinkLedgerSink`` (ruling 67).

        There is exactly one binding slot; resolving the sink from it keeps a
        second slot from disagreeing with the gate about which service is
        live.  An unbound slot, or a permissive test double that does not
        implement the callbacks, yields None.
        """

        admission = getattr(self, "_link_admission", None)
        gate = getattr(admission, "gate", None)
        if gate is None:
            return None
        return gate if isinstance(gate, LinkLedgerSink) else None

    # -- writes used by existing transactions ---------------------------

    def _ledger_open_locked(self, row: LinkLedgerOpen) -> None:
        self._db.execute(
            """INSERT OR IGNORE INTO link_ledger (
                   actor, message_id, direction, peer, intent, reply_to, state,
                   opened_at_ms, expires_at_ms
               ) VALUES (?, ?, 'out', ?, ?, ?, ?, ?, ?)""",
            (
                row.actor,
                row.message_id,
                row.peer,
                row.intent,
                row.reply_to,
                row.state,
                row.opened_at_ms,
                row.expires_at_ms,
            ),
        )

    def _ledger_sent_locked(self, sender: str, message_id: str, now_ms: int) -> None:
        """Stamp the real send moment; the first one wins (B7)."""

        self._db.execute(
            """UPDATE link_ledger SET sent_at_ms = COALESCE(sent_at_ms, ?)
                WHERE actor = ? AND message_id = ? AND direction = 'out'""",
            (now_ms, sender, message_id),
        )

    def _ledger_sent_for_outbox_locked(self, message_id: str, now_ms: int) -> None:
        row = self._db.execute(
            "SELECT sender FROM outbox WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is not None:
            self._ledger_sent_locked(str(row["sender"]), message_id, now_ms)

    def _ledger_release_out_locked(self, actor: str, message_id: str) -> None:
        self._db.execute(
            "DELETE FROM link_ledger "
            "WHERE actor = ? AND message_id = ? AND direction = 'out'",
            (actor, message_id),
        )

    def _ledger_close_inbound_locked(
        self, actor: str, message_id: str, now_ms: int
    ) -> bool:
        """Close the original inbound row a reply settles (B5, §3.9)."""

        cursor = self._db.execute(
            f"""UPDATE link_ledger
                   SET state = 'closed',
                       event_at_ms = ?,
                       sample_pending = CASE WHEN state = 'open' THEN 1 ELSE 0 END
                 WHERE actor = ? AND message_id = ? AND direction = 'in'
                   AND state IN ({_placeholders(_LIVE_INBOUND_STATES)})""",
            (now_ms, actor, message_id, *_LIVE_INBOUND_STATES),
        )
        return cursor.rowcount > 0

    def _ledger_close_outbound_locked(
        self, actor: str, reply_to: str, peer: str, now_ms: int
    ) -> bool:
        """Close the outbound row an arriving reply answers (B7, §3.9)."""

        cursor = self._db.execute(
            """UPDATE link_ledger
                  SET state = 'closed', event_at_ms = ?, sample_pending = 1
                WHERE actor = ? AND message_id = ? AND direction = 'out'
                  AND peer = ? AND state = 'open'""",
            (now_ms, actor, reply_to, peer),
        )
        return cursor.rowcount > 0

    def _ledger_ack_inbound_locked(self, actor: str, message_id: str) -> None:
        self._db.execute(
            f"""UPDATE link_ledger SET state = 'acked'
                 WHERE actor = ? AND message_id = ? AND direction = 'in'
                   AND state IN ({_placeholders(_LIVE_INBOUND_STATES)})""",
            (actor, message_id, *_LIVE_INBOUND_STATES),
        )

    def _ledger_expire_inbound_locked(self, actor: str, message_id: str) -> None:
        self._db.execute(
            f"""UPDATE link_ledger SET state = 'expired'
                 WHERE actor = ? AND message_id = ? AND direction = 'in'
                   AND state IN ({_placeholders(_LIVE_INBOUND_STATES)})""",
            (actor, message_id, *_LIVE_INBOUND_STATES),
        )

    def _ledger_active_out_locked(self, actor: str, message_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM link_ledger "
            "WHERE actor = ? AND message_id = ? AND direction = 'out'",
            (actor, message_id),
        ).fetchone()
        return row is not None

    def _write_inbound_ledger_locked(
        self,
        *,
        actor: str,
        message: InboxMessage,
        received_at_ms: int,
        expires_at_ms: int,
    ) -> bool:
        """Record one injected inbound row (B8).  Idempotent per message id."""

        cursor = self._db.execute(
            """INSERT OR IGNORE INTO link_ledger (
                   actor, message_id, direction, peer, intent, reply_to, state,
                   opened_at_ms, expires_at_ms
               ) VALUES (?, ?, 'in', ?, ?, ?, 'open', ?, ?)""",
            (
                actor,
                message.message_id,
                message.sender,
                message.intent,
                _link_reply_to(message),
                received_at_ms,
                expires_at_ms,
            ),
        )
        return cursor.rowcount > 0

    # -- the one transactional update command ---------------------------

    @_synchronized
    def update_link_ledger(
        self, command: UpdateLinkLedgerCommand
    ) -> LinkLedgerResult:
        now = self._now_ms() if command.now_ms is None else command.now_ms
        ttl = self.durable_ttl_ms
        released: list[str] = []
        expired: list[str] = []
        failed: list[str] = []
        drained: list[tuple[str, str]] = []
        cancelled: list[str] = []
        purged = False
        with self._db:
            for message_id in command.release:
                outcome = self._release_one_locked(message_id, now, ttl)
                if outcome == "released":
                    released.append(message_id)
                elif outcome == "expired":
                    expired.append(message_id)
                elif outcome is not None:
                    failed.append(message_id)
            for actor, message_ids in command.drain.items():
                for message_id in message_ids:
                    if self._drain_one_locked(actor, message_id, now, ttl):
                        drained.append((actor, message_id))
            for message_id, reason in command.fail.items():
                if self._fail_held_locked(message_id, reason, now):
                    failed.append(message_id)
            for message_id, reason in command.cancel_outbound.items():
                if self._cancel_one_locked(message_id, reason, now):
                    cancelled.append(message_id)
            for actor, message_id in command.release_outbound:
                self._ledger_release_out_locked(actor, message_id)
            for message_id, reason in command.reasons.items():
                self._db.execute(
                    "UPDATE outbox SET link_hold = ? "
                    "WHERE message_id = ? AND link_hold IS NOT NULL",
                    (reason, message_id),
                )
            for record in command.censor:
                self._db.execute(
                    """UPDATE link_ledger
                          SET state = 'censored', threshold_ms = ?,
                              expires_at_ms = ?
                        WHERE actor = ? AND message_id = ? AND direction = 'in'
                          AND state = 'open'""",
                    (
                        record.threshold_ms,
                        record.expires_at_ms,
                        record.actor,
                        record.message_id,
                    ),
                )
            for actor, message_id in command.sampled:
                self._db.execute(
                    "UPDATE link_ledger SET sample_pending = 0 "
                    "WHERE actor = ? AND message_id = ?",
                    (actor, message_id),
                )
            for row in command.open_outbound:
                self._ledger_open_locked(row)
            for actor, message_id, event_at_ms in command.close_inbound:
                self._ledger_close_inbound_locked(actor, message_id, event_at_ms)
            for actor, peer, task_id, bound_at_ms in command.bind_tasks:
                self._db.execute(
                    """INSERT OR IGNORE INTO link_task_bindings
                           (actor, peer, task_id, bound_at_ms)
                       VALUES (?, ?, ?, ?)""",
                    (actor, peer, task_id, bound_at_ms),
                )
            for actor, peer, task_id in command.unbind_tasks:
                self._db.execute(
                    """DELETE FROM link_task_bindings
                        WHERE actor = ? AND peer = ? AND task_id = ?""",
                    (actor, peer, task_id),
                )
            if command.purge_before_ms is not None:
                purged = self._db.execute(
                    f"""DELETE FROM link_ledger
                         WHERE expires_at_ms < ?
                           AND state IN ({_placeholders(_TERMINAL_STATES)})""",
                    (command.purge_before_ms, *_TERMINAL_STATES),
                ).rowcount > 0
        _record_committed_change(self, purged)
        return LinkLedgerResult(
            released=tuple(released),
            expired=tuple(expired),
            failed=tuple(failed),
            drained=tuple(drained),
            cancelled=tuple(cancelled),
        )

    def _release_one_locked(
        self, message_id: str, now_ms: int, ttl_ms: int
    ) -> str | None:
        """Release one held row; "released", "expired", "failed" or None."""

        row = self._db.execute(
            "SELECT * FROM outbox WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        if int(row["expires_at_ms"]) <= now_ms:
            # The retry pump owns the TTL terminal; never release it here.
            return "expired"
        if row["link_hold"] is None:
            return None
        ledger = self._db.execute(
            "SELECT actor FROM link_ledger "
            "WHERE message_id = ? AND direction = 'out'",
            (message_id,),
        ).fetchone()
        # The reply association comes from the outbox row itself (rework 1):
        # a held reply books no ledger ``out`` row, so the old ledger lookup
        # never saw it and its original stayed pending forever (P1).
        reply_to = _outbox_reply_target(row)
        if str(row["intent"]) == "reply" and reply_to:
            actor = str(row["sender"])
            if not self._reply_original_pending_locked(actor, reply_to):
                return self._fail_reply_locked(actor, message_id, now_ms)
            # B5's settlement protocol, in this one transaction: close the
            # inbound row, ack the original, then clear the hold.  A missing
            # ledger row only costs the sample -- the inbox row is the
            # authority on whether the question still awaits its reply.
            self._ledger_close_inbound_locked(actor, reply_to, now_ms)
            self._ack_locked(actor, reply_to, now_ms)
        self._db.execute(
            "UPDATE outbox SET link_hold = NULL, next_attempt_ms = ? "
            "WHERE message_id = ?",
            (now_ms, message_id),
        )
        if ledger is not None:
            self._db.execute(
                """UPDATE link_ledger SET state = 'open'
                    WHERE actor = ? AND message_id = ? AND direction = 'out'
                      AND state = 'held'""",
                (str(ledger["actor"]), message_id),
            )
        return "released"

    def _reply_original_pending_locked(self, actor: str, reply_to: str) -> bool:
        """Is the original request still awaiting its reply in the inbox?"""

        row = self._db.execute(
            "SELECT 1 FROM inbox "
            "WHERE recipient = ? AND message_id = ? AND consumed = 0",
            (actor, reply_to),
        ).fetchone()
        return row is not None

    def _fail_reply_locked(self, actor: str, message_id: str, now_ms: int) -> str:
        """A held reply whose original request is gone cannot be released (B5)."""

        original = self._reply_target_locked(actor, message_id)
        drained = None
        if original:
            drained = self._db.execute(
                "SELECT 1 FROM link_ledger "
                "WHERE actor = ? AND message_id = ? AND direction = 'in' "
                "AND state = 'drained'",
                (actor, original),
            ).fetchone()
        code = (
            ipc_errors.LINK_RECLAIMED
            if drained is not None
            else ipc_errors.MESSAGE_REPLY_UNAVAILABLE
        )
        self._outbox_to_dlq_by_id_locked(message_id, code, now_ms)
        return "failed"

    def _reply_target_locked(self, actor: str, message_id: str) -> str:
        row = self._db.execute(
            "SELECT payload, idempotency_key FROM outbox WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            return ""
        return _outbox_reply_target(row) or ""

    def _drain_one_locked(
        self, actor: str, message_id: str, now_ms: int, ttl_ms: int
    ) -> bool:
        # Durable before any ack: a failed update is retried, never silently
        # treated as "the reply may be sent again" (B10).
        cursor = self._db.execute(
            f"""UPDATE link_ledger
                   SET state = 'drained', event_at_ms = ?,
                       expires_at_ms = ?, sample_pending = 0
                 WHERE actor = ? AND message_id = ? AND direction = 'in'
                   AND state IN ({_placeholders(_LIVE_INBOUND_STATES)})""",
            (now_ms, now_ms + ttl_ms, actor, message_id, *_LIVE_INBOUND_STATES),
        )
        created = cursor.rowcount > 0
        if cursor.rowcount == 0:
            existing = self._db.execute(
                "SELECT state, expires_at_ms FROM link_ledger "
                "WHERE actor = ? AND message_id = ? AND direction = 'in'",
                (actor, message_id),
            ).fetchone()
            if existing is None:
                # R22 (E26-08): a reclaimed link's request may still be waiting
                # in the inbox with no ledger row -- it was never injected.  Book
                # the terminal row now so the drain is durable and a restart
                # cannot resurrect the request.
                if not self._drain_unledgered_locked(
                    actor, message_id, now_ms, ttl_ms
                ):
                    return False
                created = True
            elif str(existing["state"]) == "drained":
                created = True
            else:
                return False
        self._db.execute(
            "UPDATE link_ledger SET expires_at_ms = ? "
            "WHERE actor = ? AND message_id = ? AND direction = 'in'",
            (now_ms + ttl_ms, actor, message_id),
        )
        self._ack_locked(actor, message_id, now_ms, only_if_pending=False)
        forget = getattr(self, "_forget_admission", None)
        if callable(forget):
            forget(message_id)
        return created

    def _drain_unledgered_locked(
        self, actor: str, message_id: str, now_ms: int, ttl_ms: int
    ) -> bool:
        """Book the drained terminal for a row the ledger never saw (R22).

        The request is durable in the inbox but was never injected, so there
        is nothing to update.  ``peer``/``intent`` come from the inbox row; a
        missing row means there is nothing durable to settle at all.
        """

        row = self._db.execute(
            "SELECT sender, intent, received_at_ms, expires_at_ms FROM inbox "
            "WHERE recipient = ? AND message_id = ?",
            (actor, message_id),
        ).fetchone()
        if row is None:
            return False
        expires = row["expires_at_ms"]
        self._db.execute(
            """INSERT OR IGNORE INTO link_ledger (
                   actor, message_id, direction, peer, intent, reply_to, state,
                   opened_at_ms, event_at_ms, expires_at_ms
               ) VALUES (?, ?, 'in', ?, ?, NULL, 'drained', ?, ?, ?)""",
            (
                actor,
                message_id,
                str(row["sender"]),
                str(row["intent"]),
                int(row["received_at_ms"]),
                now_ms,
                int(expires) if expires is not None else now_ms + ttl_ms,
            ),
        )
        return True

    def _fail_held_locked(self, message_id: str, code: str, now_ms: int) -> bool:
        row = self._db.execute(
            "SELECT * FROM outbox WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None or row["link_hold"] is None:
            return False
        self._outbox_to_dlq_locked(row, code, now_ms)
        return True

    def _cancel_one_locked(self, message_id: str, code: str, now_ms: int) -> bool:
        row = self._db.execute(
            "SELECT * FROM outbox WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None or row["link_hold"] is not None:
            return False
        self._outbox_to_dlq_locked(row, code, now_ms)
        return True

    def _outbox_to_dlq_by_id_locked(
        self, message_id: str, code: str, now_ms: int
    ) -> None:
        row = self._db.execute(
            "SELECT * FROM outbox WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is not None:
            self._outbox_to_dlq_locked(row, code, now_ms)

    def _ack_locked(
        self,
        recipient: str,
        message_id: str,
        now_ms: int,
        *,
        only_if_pending: bool = True,
    ) -> None:
        predicate = "AND consumed = 0" if only_if_pending else ""
        self._db.execute(
            f"""UPDATE inbox SET consumed = 1, acknowledged_at_ms = ?
                 WHERE recipient = ? AND message_id = ? {predicate}""",  # noqa: S608
            (now_ms, recipient, message_id),
        )
        self._ledger_ack_inbound_locked(recipient, message_id)

    # -- post-commit sink callbacks -------------------------------------

    def _sink_injected(self, actor: str, message: InboxMessage, now_ms: int) -> None:
        sink = self._link_ledger_sink()
        if sink is None:
            return
        try:
            sink.injected(
                actor=actor,
                message_id=message.message_id,
                sender=message.sender,
                intent=message.intent,
                reply_to=_link_reply_to(message),
                received_at_ms=now_ms,
            )
        except Exception:  # noqa: BLE001 - memory-only notification
            pass

    def _sink_reply_received(
        self, actor: str, message: InboxMessage, now_ms: int
    ) -> None:
        reply_to = _link_reply_to(message)
        if reply_to is None:
            return
        sink = self._link_ledger_sink()
        if sink is None:
            return
        try:
            sink.reply_received(
                actor=actor,
                reply_message_id=message.message_id,
                reply_to=reply_to,
                sender=message.sender,
                received_at_ms=now_ms,
            )
        except Exception:  # noqa: BLE001 - memory-only notification
            pass


def _placeholders(values: tuple[str, ...]) -> str:
    return ", ".join("?" for _ in values)
