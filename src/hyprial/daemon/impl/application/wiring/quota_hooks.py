"""Unified hooks H4 wiring: the quota consumer replaces the two callbacks."""

from __future__ import annotations

from typing import Any


def wire_quota_hooks(
    *,
    usage_cache: Any,
    watchdog: Any,
    consumer_factory: Any,
    daemon_hooks: Any,
) -> None:
    """Register quota on the bus; the two daemon callbacks are deleted.

    Two registrations replace them exactly: the usage-cache refresh observer
    now publishes ``usage.refreshed`` at the same observation point (design
    §4), and the watchdog registers as the daemon-level ``quota`` lane for
    ``turn.failed`` + ``usage.refreshed`` (design §3).  Neither lane may
    change settlement, routing or any agent's harness/model/session
    (Allen 09-27).
    """

    if usage_cache is not None:
        usage_cache.set_refresh_observer(daemon_hooks.observe_usage_refreshed)
    if watchdog is not None and consumer_factory is not None:
        daemon_hooks.register_consumer(
            "quota",
            consumer_factory(watchdog),
            ("turn.failed", "usage.refreshed"),
        )


__all__ = ["wire_quota_hooks"]
