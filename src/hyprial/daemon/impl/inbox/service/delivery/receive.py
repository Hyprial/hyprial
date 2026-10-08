from __future__ import annotations
import json
from uuid import NAMESPACE_URL, uuid5
from hyprial.daemon.impl.inbox.contracts.api  import (
    DeliveryLifecycle,
    InboxMessage,
    ReceiveResult,
)
from hyprial.daemon.impl.inbox.links.pull  import (
    HoldReason,
    TerminalState,
)
from hyprial.daemon.impl.inbox.service.delivery.notices import _bare_sender
from hyprial.daemon.impl.inbox.service.synchronization import _synchronized

"""SQLite-backed outbox, inbox, deduplication, FIFO, DLQ and custody state."""

class InboxServiceReceiveMixin:
    @_synchronized
    def receive(
        self, message: InboxMessage, *, now_ms: int | None = None
    ) -> ReceiveResult:
        now = self._now_ms() if now_ms is None else now_ms
        if _bare_sender(message.sender):
            # Ingress admission gate (Allen B): after the daemon send
            # boundary canonicalizes every sender, a bare (node-shaped)
            # sender on the wire means a pre-gate or bypassing client.
            # Refuse to commit — no row, so no receipt is ever signed — and
            # tell the origin why.  Never silent on either side: the
            # sender's unreceipted retries continue until ACK or TTL,
            # and the notice states the reason.
            return self._reject_bare_sender(message, now)
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
                """INSERT INTO inbox (
                       message_id, conversation_id, sender, recipient, payload, intent,
                       lifecycle, idempotency_key, created_at_ms, received_at_ms,
                       message_expires_at_ms, expires_at_ms, origin_node
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
        wake = message.intent in {"request", "reply"}
        return ReceiveResult(
            message.message_id,
            accepted=True,
            acknowledged=True,
            wake=wake,
            auto_reply=message.intent == "request",
        )
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
