"""The quota hook consumer: ``turn.failed`` + ``usage.refreshed`` drive the watchdog.

Design §3 (unified hooks, H4): the quota policy no longer hangs off two
daemon callbacks (usage-cache refresh and the failed-turn settlement
observer).  It subscribes to two bus events instead:

- ``turn.failed`` with ``failureCode == PROVIDER_USAGE_LIMIT`` -- the alert
  names the failing agent (``event.actor``) exactly as the old settlement
  callback named the recipient;
- ``usage.refreshed`` -- one reading evaluation plus one hold flush, in the
  same order the old refresh callback ran them.

The consumer is wired by the shell into the daemon-owned bus; it never runs
in an actor mailbox (the bus gives it one isolated lane) and it never
changes how an exhausted agent's turns are settled (Allen 09-17/09-27: the
daemon only reports facts and suggests; it never re-routes an agent).
"""

from __future__ import annotations

from typing import Any, Protocol

from hyprial.kernel import HookEvent

#: The failure code that feeds the turn-failure alert (design §3).
#: ``PROVIDER_RATE_LIMITED`` is deliberately not aliased to it: a 429 window
#: resets on its own, so it is not evidence that the account's quota is gone
#: and must not raise the ``额度耗尽`` owner alert (2026-10-16 acceptance,
#: resident pi credential acceptance, item 1).
USAGE_LIMIT_FAILURE_CODE = "PROVIDER_USAGE_LIMIT"
#: What the quota lane subscribes to on the daemon bus.
QUOTA_HOOK_EVENTS: tuple[str, ...] = ("turn.failed", "usage.refreshed")


class QuotaWatchdogAuthority(Protocol):
    """The daemon-owned durable authority this consumer drives.

    ``AsyncQuotaWatchdog`` satisfies this structurally; the consumer holds no
    daemon import (the shell wires the two together).
    """

    def observe_readings(self) -> Any: ...

    def observe_usage_limit_failure(self, actor: str) -> Any | None: ...

    def flush_failures(self) -> Any | None: ...


class QuotaHookConsumer:
    """One bus lane: turn failures and usage refreshes reach the watchdog."""

    def __init__(self, watchdog: QuotaWatchdogAuthority) -> None:
        self._watchdog = watchdog

    def observe(self, event: HookEvent) -> None:
        if event.event == "usage.refreshed":
            # Same order as the deleted refresh callback: evaluate the new
            # readings, then give held agents their one chance to flush.
            self._watchdog.observe_readings()
            self._watchdog.flush_failures()
        elif (
            event.event == "turn.failed"
            and event.actor
            and event.payload.get("failureCode") == USAGE_LIMIT_FAILURE_CODE
        ):
            self._watchdog.observe_usage_limit_failure(event.actor)


__all__ = [
    "QUOTA_HOOK_EVENTS",
    "USAGE_LIMIT_FAILURE_CODE",
    "QuotaHookConsumer",
    "QuotaWatchdogAuthority",
]
