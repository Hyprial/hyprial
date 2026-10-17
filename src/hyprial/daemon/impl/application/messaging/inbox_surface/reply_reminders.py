"""WP7: reminder reads and the reply-reminder system notice.

Design-send-links §4.5 and §8.2.  This module is the daemon's side of the
reminder seam:

- :func:`open_requests_by_sender` answers ``message.send``: which requests
  from the named targets are still pending in the sender's inbox.
- :class:`InboxReplyReminders` implements the ``ReplyReminderPort`` the biz
  consumer drives: it reads the daemon-authoritative pending set and writes
  one system notice per newly-unanswered group.

Both only read what the daemon already decided.  They never ack, reply,
re-send a body, mint or reclaim a link, or touch W/cwnd/controller samples.
A SQLite read failure is translated to ``InboxAuthorityUnavailable`` here so
neither the optional send field nor the reminder lane can leak a storage
exception into the caller.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Collection, Sequence
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from hyprial.daemon.impl.inbox import InboxAuthorityUnavailable
from hyprial.daemon.impl.inbox.contracts.api import (
    DeliveryLifecycle,
    InboxMessage,
)


def open_requests_by_sender(
    inbox: Any,
    *,
    recipient: str,
    senders: Collection[str],
) -> dict[str, tuple[str, ...]]:
    """Pending requests in ``recipient``'s inbox, grouped by sender.

    Reads ``pending_messages`` once.  Only ``intent == "request"`` rows count
    and each group keeps arrival order (the read is ``ORDER BY arrival_id``).
    """

    wanted = set(senders)
    if not wanted:
        return {}
    try:
        # The inbox applies its own clock; this read takes no time argument.
        pending = inbox.pending_messages(recipient)
    except sqlite3.OperationalError as error:
        raise InboxAuthorityUnavailable(str(error)) from error
    grouped: dict[str, list[str]] = {}
    for message in pending:
        if message.intent != "request" or message.sender not in wanted:
            continue
        grouped.setdefault(message.sender, []).append(message.message_id)
    return {sender: tuple(ids) for sender, ids in grouped.items()}


class InboxReplyReminders:
    """The ``ReplyReminderPort`` implementation over the durable inbox."""

    def __init__(
        self,
        inbox: Any,
        *,
        node_id: str,
        clock_ms: Callable[[], int],
        logger: Callable[..., None] | None = None,
    ) -> None:
        self._inbox = inbox
        self._node_id = node_id
        self._clock_ms = clock_ms
        self._logger = logger

    def unreplied_requests(
        self,
        actor: str,
        *,
        delivery_ids: Collection[str],
        fetched_since_ms: int | None,
        fetched_until_ms: int,
        since_inclusive: bool,
    ) -> tuple[str, ...]:
        """Turn-injected requests that are still pending, in arrival order."""

        now_ms = self._clock_ms()
        try:
            pending = self._inbox.pending_messages(actor, now_ms=now_ms)
            fetched = (
                ()
                if fetched_since_ms is None
                else self._inbox.fetched_between(
                    actor,
                    since_ms=fetched_since_ms,
                    until_ms=fetched_until_ms,
                    since_inclusive=since_inclusive,
                )
            )
        except sqlite3.OperationalError as error:
            raise InboxAuthorityUnavailable(str(error)) from error
        candidates = set(delivery_ids) | set(fetched)
        if not candidates:
            return ()
        return tuple(
            message.message_id
            for message in pending
            if message.intent == "request" and message.message_id in candidates
        )

    def post_reply_reminder(
        self, actor: str, request_ids: Sequence[str], text: str
    ) -> bool:
        """Write the reminder notice; ``INSERT OR IGNORE`` on a deterministic id.

        The same ``(actor, sorted request ids)`` group maps to the same
        ``message_id``, so an undrained notice absorbs a duplicate write.
        """

        request_ids = tuple(request_ids)
        message_id = str(
            uuid5(
                NAMESPACE_URL,
                "hyprial:reply-reminder:"
                f"{actor}:{','.join(sorted(request_ids))}",
            )
        )
        notice = InboxMessage(
            message_id=message_id,
            conversation_id=message_id,
            sender=f"system:hyprial:{self._node_id}",
            recipient=actor,
            payload=json.dumps(
                {
                    "message": text,
                    "systemNotice": True,
                    "reason": "reply-reminder",
                    "openRequests": list(request_ids),
                }
            ).encode(),
            intent="system",
            lifecycle=DeliveryLifecycle.ONLINE_ONLY,
            idempotency_key=f"reply-reminder:{message_id}",
            created_at_ms=self._clock_ms(),
            summary=text,
        )
        return self._inbox.receive_system_notice(notice)

    def log(self, level: str, event: str, **fields: Any) -> None:
        if self._logger is not None:
            self._logger(level, "reminder", event, **fields)


__all__ = ["InboxReplyReminders", "open_requests_by_sender"]
