"""Held-overdue reminders for PAC graphs (#1803), owned by PAC.

Why this lives beside PAC and not beside the maintenance scheduler: the fact
being watched (a graph is ``held`` and nobody is acting on it) and the channel
that fixes it (a durable PAC notification to its creator) are both PAC's.  The
kernel scheduler stays generic; PAC borrows one timer key from it.

The reminder is a *reminder*: it never resets a node, cancels a graph or
restarts a worker.  What it writes is one recorded notification row (plus its
``notification_planned`` journal fact) that PAC's normal drain delivers; a
deterministic notice id keeps that write idempotent across restarts and caps it
at one reminder per graph per 24h.
"""

from __future__ import annotations

from hyprial.daemon.impl.pac.held_reminder.core import HeldReminder
from hyprial.daemon.impl.pac.held_reminder.detection import (
    HELD_REMINDER_EDGE,
    HELD_REMINDER_EVENT_PREFIX,
    BlockingNode,
    HeldGraph,
    held_duration_text,
    scan_held_graphs,
)
from hyprial.daemon.impl.pac.held_reminder.types import (
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_REPEAT_WINDOW_MS,
    DEFAULT_THRESHOLD_MS,
    HELD_REMINDER_KEY,
    HeldReminderConfig,
)

__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_REPEAT_WINDOW_MS",
    "DEFAULT_THRESHOLD_MS",
    "HELD_REMINDER_EDGE",
    "HELD_REMINDER_EVENT_PREFIX",
    "HELD_REMINDER_KEY",
    "BlockingNode",
    "HeldGraph",
    "HeldReminder",
    "HeldReminderConfig",
    "held_duration_text",
    "scan_held_graphs",
]
