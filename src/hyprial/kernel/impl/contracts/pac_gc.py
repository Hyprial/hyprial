"""Shared PAC garbage-collection cadence and batch budgets."""

from __future__ import annotations

# One registered source for the daemon service and its tests.  The collector
# owns a timer thread, not a transport callback or the PAC actor queue.
PAC_GC_INTERVAL_SECONDS = 5 * 60.0
PAC_GC_JITTER_RATIO = 0.1
PAC_GC_MAX_REMOVALS_PER_PASS = 50

__all__ = [
    "PAC_GC_INTERVAL_SECONDS",
    "PAC_GC_JITTER_RATIO",
    "PAC_GC_MAX_REMOVALS_PER_PASS",
]
