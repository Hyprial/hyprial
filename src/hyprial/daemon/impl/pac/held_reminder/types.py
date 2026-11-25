"""Bounded configuration for the held-graph reminder (§#1803).

The switch and the threshold are operator-visible knobs; both read the daemon's
environment once at startup so a restart is the only thing that changes them
(the cadence itself is a constant a test may override).
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from dataclasses import dataclass

HOUR_MS = 3_600_000

#: The scheduler key this feature owns.  One key, one live timer.
HELD_REMINDER_KEY = "pac-held-reminder"

#: Scan cadence.  A held graph is a slow fact; an hour is enough to nudge.
DEFAULT_INTERVAL_SECONDS = 3600.0

#: A graph must be held at least this long before its creator hears about it.
DEFAULT_THRESHOLD_MS = 6 * HOUR_MS

#: At most one reminder per graph inside this window.
DEFAULT_REPEAT_WINDOW_MS = 24 * HOUR_MS

_ENABLED_ENV = "HYPRIAL_PAC_HELD_REMINDER"
_THRESHOLD_ENV = "HYPRIAL_PAC_HELD_REMINDER_THRESHOLD_SECONDS"

_TRUTHY = frozenset({"1", "true", "on", "yes"})
_FALSY = frozenset({"0", "false", "off", "no"})


def _positive_seconds(raw: str | None, default_ms: int) -> int:
    """Parse a seconds override; an unreadable one keeps the default."""

    if raw is None or not raw.strip():
        return default_ms
    try:
        seconds = float(raw.strip())
    except ValueError:
        return default_ms
    if seconds <= 0 or seconds != seconds or seconds == float("inf"):
        return default_ms
    return int(seconds * 1000)


@dataclass(frozen=True, slots=True)
class HeldReminderConfig:
    """Whether to remind, how overdue a hold must be, and how often to look."""

    enabled: bool = True
    threshold_ms: int = DEFAULT_THRESHOLD_MS
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    repeat_window_ms: int = DEFAULT_REPEAT_WINDOW_MS

    def __post_init__(self) -> None:
        if self.threshold_ms < 0:
            raise ValueError("held reminder threshold must not be negative")
        if self.interval_seconds <= 0:
            raise ValueError("held reminder interval must be positive")
        if self.repeat_window_ms <= 0:
            raise ValueError("held reminder repeat window must be positive")

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "HeldReminderConfig":
        source = os.environ if environ is None else environ
        raw_switch = str(source.get(_ENABLED_ENV, "")).strip().lower()
        return cls(
            enabled=raw_switch not in _FALSY,
            threshold_ms=_positive_seconds(
                source.get(_THRESHOLD_ENV), DEFAULT_THRESHOLD_MS
            ),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "thresholdMs": self.threshold_ms,
            "intervalSeconds": self.interval_seconds,
            "repeatWindowMs": self.repeat_window_ms,
        }
