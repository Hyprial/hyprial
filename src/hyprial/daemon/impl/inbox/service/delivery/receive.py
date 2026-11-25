from __future__ import annotations
import json
from uuid import NAMESPACE_URL, uuid5
from hyprial.kernel import ipc_errors, parse_user_uri
from hyprial.daemon.impl.inbox.contracts.api  import (
    DeliveryLifecycle,
    InboxMessage,
    ReceiveResult,
    SubmissionResult,
)
from hyprial.daemon.impl.inbox.links.pull  import (
    HoldReason,
    TerminalState,
)
from hyprial.daemon.impl.inbox.service.delivery.notices import (
    _ExpiredSenderNotice,
    _bare_sender,
)
from hyprial.daemon.impl.inbox.service.synchronization import _synchronized
from hyprial.daemon.impl.inbox.envelope.columns import (
    ENVELOPE_COLUMN_LIST,
    ENVELOPE_PLACEHOLDERS,
    envelope_values,
)
from hyprial.daemon.impl.inbox.envelope.payload import adopt_payload_envelope

"""SQLite-backed outbox, inbox, deduplication, FIFO, DLQ and custody state."""

class InboxServiceReceiveMixin:
    @_synchronized
    def receive(
        self, message: InboxMessage, *, now_ms: int | None = None
    ) -> ReceiveResult:
        now = self._now_ms() if now_ms is None else now_ms
        message = adopt_payload_envelope(message)
        if _bare_sender(message.sender):
            # Ingress admission gate (Allen B): after the daemon send
            # boundary canonicalizes every sender, a bare (node-shaped)
            # sender on the wire means a pre-gate or bypassing client.
            # Refuse to commit — no row, so no receipt is ever signed — and
            # tell the origin why.  Never silent on either side: the
            # sender's unreceipted retries continue until ACK or TTL,
            # and the notice states the reason.
            return self._reject_bare_sender(message, now)
        unclaimed = self._recipient_unclaimed(message)
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
                    reason=HoldReason.ACK_RECEIVED,
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
            if not unclaimed:
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
                    f"""INSERT INTO inbox (
                           message_id, conversation_id, sender, recipient, payload, intent,
                           lifecycle, idempotency_key, created_at_ms, received_at_ms,
                           message_expires_at_ms, expires_at_ms, origin_node, summary,
                           arrival_ordinal, {ENVELOPE_COLUMN_LIST}
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                 {ENVELOPE_PLACEHOLDERS})""",
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
                        message.expires_at_ms,
                        # Ordinary TTLs count from this node's receive clock.  An
                        # explicit producer deadline is kept exactly, including
                        # when it is later than the ordinary hold window.
                        (
                            message.expires_at_ms
                            if message.expires_at_ms is not None
                            else now + self.durable_ttl_ms
                        ),
                        message.origin_node,
                        message.summary,
                        self._next_arrival_ordinal(),
                        *envelope_values(message),
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
                    reason=HoldReason.ACK_RECEIVED,
                    now_ms=now,
                )
        if unclaimed:
            return self._unclaimed_recipient_refusal(message, now)
        wake = message.intent in {"request", "reply"}
        if wake and parse_user_uri(message.recipient) is not None:
            self._human_wake_gate.observe(message, path="inbox.receive")
        return ReceiveResult(
            message.message_id,
            accepted=True,
            acknowledged=True,
            wake=wake,
            reply_owed=message.intent == "request",
        )

    def _recipient_unclaimed(self, message: InboxMessage) -> bool:
        """Whether the injected ``recipient_claimed`` rejects the recipient.

        The daemon injects the predicate (UUID addressing §2.3); without it
        every recipient is claimed and receive is unchanged.  receive asks
        before its write transaction, so the predicate's registry, desired
        state and presence reads never run under the inbox write lock, and
        uses the answer only after its duplicate checks, so a message already
        received keeps its duplicate ACK.
        """

        claimed = self._recipient_claimed
        return claimed is not None and not claimed(message.recipient)

    def _unclaimed_recipient_refusal(
        self, message: InboxMessage, now_ms: int
    ) -> ReceiveResult:
        """End a message whose recipient nothing here claims (UUID §2.3).

        A copy this node still holds (the local sender's outbox row, or a
        custody row the mailbox pump is delivering) moves to the DLQ with
        ``TARGET_UNRESOLVED``; a network arrival is refused like any ingress
        refusal: a terminal ``refused`` record, no inbox row, no receipt,
        and a notice to the sender staged after the record commits.
        """

        reason = ipc_errors.TARGET_UNRESOLVED
        held = {
            table: self._db.execute(
                f"SELECT * FROM {table} WHERE message_id = ? AND recipient = ?",  # noqa: S608
                (message.message_id, message.recipient),
            ).fetchone()
            for table in ("outbox", "custody")
        }
        if held["outbox"] is not None:
            self._outbox_to_dlq(held["outbox"], reason, now_ms)
        elif held["custody"] is not None:
            self._custody_to_dlq(held["custody"], reason, now_ms)
        else:
            with self._db:
                self._record_terminal(
                    message,
                    state=TerminalState.REFUSED,
                    reason=reason,
                    now_ms=now_ms,
                )
            self._stage_unclaimed_notice(message, now_ms=now_ms)
        self._log_ingress_refusal(message, reason)
        return ReceiveResult(
            message.message_id, accepted=False, acknowledged=False, code=reason
        )

    def _unclaimed_dead_letter(
        self, message_id: str, table: str
    ) -> SubmissionResult | None:
        """The submission outcome when receive dead-lettered a held copy.

        After a failed local delivery, a ``table`` row that is gone with
        ``TARGET_UNRESOLVED`` as its latest DLQ reason ended in
        ``_unclaimed_recipient_refusal``; the pump reports that terminal
        code instead of ``queued``.  Without an injected predicate this is
        always None.
        """

        if self._recipient_claimed is None:
            return None
        if self._db.execute(
            f"SELECT 1 FROM {table} WHERE message_id = ?",  # noqa: S608
            (message_id,),
        ).fetchone() is not None:
            return None
        row = self._db.execute(
            "SELECT reason FROM dlq WHERE message_id = ? ORDER BY dlq_id DESC LIMIT 1",
            (message_id,),
        ).fetchone()
        if row is None or row[0] != ipc_errors.TARGET_UNRESOLVED:
            return None
        return SubmissionResult(
            message_id, False, code=ipc_errors.TARGET_UNRESOLVED
        )

    def _stage_unclaimed_notice(self, message: InboxMessage, *, now_ms: int) -> None:
        """Queue the sender's notice for an unclaimed recipient (custody actor)."""

        reason = ipc_errors.TARGET_UNRESOLVED
        text = (
            f"message {message.message_id} was refused at ingress: nothing on "
            f"node {self.node_id} claims {message.recipient!r} ({reason})"
        )
        payload = json.dumps(
            {
                "message": text,
                "originalMessageId": message.message_id,
                "reason": reason,
                "systemNotice": True,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        self._pending_link_notices.append(
            _ExpiredSenderNotice(
                original_message_id=message.message_id,
                target_node=(
                    message.origin_node or self._agent_node(message.sender)
                ),
                notice=InboxMessage(
                    message_id=str(
                        uuid5(
                            NAMESPACE_URL,
                            f"hyprial:target-unresolved:{message.message_id}",
                        )
                    ),
                    conversation_id=message.conversation_id,
                    sender=f"system:hyprial:{self.node_id}",
                    recipient=message.sender,
                    payload=payload,
                    intent="system",
                    lifecycle=DeliveryLifecycle.ONLINE_ONLY,
                    idempotency_key=f"target-unresolved:{message.message_id}",
                    created_at_ms=now_ms,
                    summary=text[:512],
                ),
            )
        )

    def take_pending_link_notices(self) -> tuple[_ExpiredSenderNotice, ...]:
        """Drain notices staged by refused receives (actor-thread only)."""

        pending = getattr(self, "_pending_link_notices", None)
        if not pending:
            return ()
        staged = tuple(pending)
        pending.clear()
        return staged

    def _log_ingress_refusal(self, message: InboxMessage, code: str) -> None:
        try:
            self._logger.log(
                "warn",
                "inbox.ingress_refused",
                code=code,
                sender=message.sender,
                recipient=message.recipient,
                messageId=message.message_id,
            )
        except Exception:  # noqa: BLE001 - logging cannot break ingress
            pass

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
