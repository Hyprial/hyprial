"""Expired-sender notice helpers."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from hyprial.daemon.impl.inbox.contracts.api import (
    DeliveryLifecycle,
    InboxMessage,
)


@dataclass(frozen=True, slots=True)
class _ExpiredSenderNotice:
    original_message_id: str
    target_node: str | None
    notice: InboxMessage


def _bare_sender(sender: str) -> bool:
    """Is this sender a bare (node-shaped) name the ingress gate must refuse?"""

    from hyprial.daemon.impl.configuration.identity import classify_target_identity
    from hyprial.kernel import TARGET_KIND_HOST

    return classify_target_identity(sender) == TARGET_KIND_HOST


class InboxServiceNoticesMixin:
    def _expired_sender_notice(
        self, row: sqlite3.Row, now_ms: int
    ) -> _ExpiredSenderNotice | None:
        if row["fetched_at_ms"] is not None:
            return None
        message = self._row_message(row)
        if message.intent == "system":
            return None
        if message.intent == "reply" and message.sender.startswith("system:"):
            return None
        # PAC requests are settled by the daemon-side prune hook (#857), which
        # fails their node with pac:request-expired. The binding lookup lives
        # in the PAC store, outside this authority, so use the workflow-
        # message-id prefix it is keyed on (a superset: alarms stay silent too).
        if message.message_id.startswith("workflow-"):
            return None
        created = self._format_notice_time(message.created_at_ms)
        text = (
            f"The message you sent to {message.recipient} at {created} "
            f"expired unread after {self.durable_ttl_ms} ms. "
            f"Message ID: {message.message_id}. "
            f"Conversation ID: {message.conversation_id}."
        )
        payload = json.dumps(
            {
                "message": text,
                "originalMessageId": message.message_id,
                "reason": "TTL_EXPIRED",
                "systemNotice": True,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        notice = InboxMessage(
            message_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"hyprial:inbox-ttl-expired:{message.message_id}:{message.sender}",
                )
            ),
            conversation_id=message.conversation_id,
            sender=f"system:hyprial:{self.node_id}",
            recipient=message.sender,
            payload=payload,
            intent="system",
            lifecycle=DeliveryLifecycle.ONLINE_ONLY,
            idempotency_key=f"inbox-ttl-expired:{message.message_id}",
            created_at_ms=now_ms,
        )
        return _ExpiredSenderNotice(
            original_message_id=message.message_id,
            target_node=message.origin_node or self._agent_node(message.sender),
            notice=notice,
        )

    @staticmethod
    def _format_notice_time(created_at_ms: int) -> str:
        try:
            return (
                datetime.fromtimestamp(created_at_ms / 1_000, UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        except (OSError, OverflowError, ValueError):
            return f"{created_at_ms} ms since the Unix epoch"

    def _deliver_expired_sender_notice(self, item: _ExpiredSenderNotice) -> None:
        delivered = False
        try:
            if item.target_node == self.node_id:
                delivered = self.receive_system_notice(item.notice)
            elif item.target_node is not None:
                delivered = self._transport.deliver_notice(
                    item.target_node,
                    item.notice,
                )
        except Exception:  # noqa: BLE001 - the notice is explicitly best-effort
            delivered = False
        self._log_expired_sender_notice(
            item.original_message_id,
            item.target_node,
            delivered,
        )

    def _log_expired_sender_notice(
        self,
        message_id: str,
        target_node: str | None,
        delivered: bool,
    ) -> None:
        try:
            self._logger.log(
                "info",
                "inbox.expired_sender_notice",
                messageId=message_id,
                targetNode=target_node,
                delivered=delivered,
            )
        except Exception:  # noqa: BLE001 - logging cannot break inbox pruning
            pass
