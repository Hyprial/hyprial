"""WP7 wiring: register the reply-reminder lane on the daemon bus.

Mirrors ``wire_quota_hooks``: the daemon owns the bus, the shell owns the biz
consumer, and the daemon never imports biz.  The event names live here so the
daemon side is self-contained; the W2 test pins them equal to the biz-side
``REPLY_REMINDER_EVENTS`` constant.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hyprial.daemon.impl.application.messaging.inbox_surface.reply_reminders import (
    InboxReplyReminders,
)
from hyprial.daemon.impl.application.ports import ReplyReminderConsumerFactory

#: The three turn events the reminder lane subscribes to (design §2.1).
REPLY_REMINDER_EVENT_NAMES: tuple[str, ...] = (
    "turn.started",
    "turn.completed",
    "turn.failed",
)


def wire_reply_reminder_hooks(
    *,
    inbox: Any,
    node_id: str,
    clock_ms: Callable[[], int],
    consumer_factory: ReplyReminderConsumerFactory | None,
    daemon_hooks: Any,
    logger: Callable[..., None] | None = None,
) -> None:
    """Register the daemon-level reminder consumer, or do nothing.

    ``consumer_factory is None`` means the shell did not supply the biz
    consumer (tests, or a build without the reminder feature): no lane is
    registered and turn events behave exactly as before.
    """

    if consumer_factory is None:
        return
    port = InboxReplyReminders(
        inbox, node_id=node_id, clock_ms=clock_ms, logger=logger
    )
    daemon_hooks.register_consumer(
        "reply-reminder", consumer_factory(port), REPLY_REMINDER_EVENT_NAMES
    )


__all__ = ["REPLY_REMINDER_EVENT_NAMES", "wire_reply_reminder_hooks"]
