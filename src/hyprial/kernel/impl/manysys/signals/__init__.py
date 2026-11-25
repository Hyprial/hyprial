# Decision record (manysys/signals, W1-MANYSYS 2026-11-21):
# - Subpackage aggregating cross-platform termination delivery and descriptor
#   stop watchers.
"""Cross-platform termination and stdin-stop primitives."""

from __future__ import annotations

from hyprial.kernel.impl.manysys.signals.stdin_stop import (
    STDIN_SHUTDOWN_LINE,
    StdinStopWatcher,
)
from hyprial.kernel.impl.manysys.signals.termination import deliver_main_termination

__all__ = [
    "STDIN_SHUTDOWN_LINE",
    "StdinStopWatcher",
    "deliver_main_termination",
]
