"""Thread-CPU cost counters keyed by a fixed name set.

Lives outside ``hyprial.daemon`` so the Agent actor (``hyprial.agents``) can
own one without importing the daemon package, whose ``__init__`` imports the
application and would make the import circular.  What the counters mean --
the attribution rule and the calibration contract -- is documented once, in
``hyprial.daemon.ipc_stats``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from typing import Any

__all__ = [
    "OTHER_KEY",
    "CallCostCounters",
]

#: The single bucket every name outside the fixed key set folds into.
OTHER_KEY = "(other)"

# Wall-time bucket upper bounds, in seconds.  Named so the payload keys and
# the thresholds cannot drift apart; ``ge100ms`` is the open-ended tail.
_WALL_BUCKET_BOUNDS: tuple[tuple[str, float], ...] = (
    ("lt1ms", 0.001),
    ("lt10ms", 0.010),
    ("lt100ms", 0.100),
)
_WALL_BUCKET_TAIL = "ge100ms"


class _Totals:
    __slots__ = ("calls", "errors", "cpu", "wall", "max_wall", "buckets")

    def __init__(self) -> None:
        self.calls = 0
        self.errors = 0
        self.cpu = 0.0
        self.wall = 0.0
        self.max_wall = 0.0
        self.buckets = [0] * (len(_WALL_BUCKET_BOUNDS) + 1)


class CallCostCounters:
    """Thread-safe aggregate of per-key thread-CPU (and optional wall) cost.

    ``wall=True`` is the IPC shape (calls, errors, threadCpuMs, wallMs,
    maxWallMs, wall buckets); ``wall=False`` is the session-actor shape
    (calls, errors, threadCpuMs), where queue wait is not the actor's cost.
    ``enabled`` is the off switch the paired overhead measurement flips; the
    call sites skip every clock read when it is False.
    """

    def __init__(self, keys: Iterable[str], *, wall: bool) -> None:
        self._keys = frozenset(keys)
        self._wall = wall
        self._lock = threading.Lock()
        self._totals: dict[str, _Totals] = {}
        self._since_ms = int(time.time() * 1000)
        self.enabled = True

    def record(
        self,
        name: str,
        *,
        cpu_seconds: float,
        wall_seconds: float = 0.0,
        error: bool = False,
    ) -> None:
        key = name if name in self._keys else OTHER_KEY
        # A thread clock cannot run backwards on one thread, but clamp anyway:
        # a negative contribution would silently hide cost from the sum the
        # calibration contract bounds.
        cpu = cpu_seconds if cpu_seconds > 0.0 else 0.0
        with self._lock:
            totals = self._totals.get(key)
            if totals is None:
                totals = self._totals[key] = _Totals()
            totals.calls += 1
            if error:
                totals.errors += 1
            totals.cpu += cpu
            if self._wall:
                totals.wall += wall_seconds
                if wall_seconds > totals.max_wall:
                    totals.max_wall = wall_seconds
                for index, (_label, bound) in enumerate(_WALL_BUCKET_BOUNDS):
                    if wall_seconds < bound:
                        totals.buckets[index] += 1
                        break
                else:
                    totals.buckets[-1] += 1

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            items = [
                (
                    key,
                    totals.calls,
                    totals.errors,
                    totals.cpu,
                    totals.wall,
                    totals.max_wall,
                    tuple(totals.buckets),
                )
                for key, totals in self._totals.items()
            ]
        result: dict[str, dict[str, Any]] = {}
        for key, calls, errors, cpu, wall, max_wall, buckets in sorted(items):
            row: dict[str, Any] = {
                "calls": calls,
                "errors": errors,
                "threadCpuMs": round(cpu * 1000, 3),
            }
            if self._wall:
                row["wallMs"] = round(wall * 1000, 3)
                row["maxWallMs"] = round(max_wall * 1000, 3)
                labels = [label for label, _bound in _WALL_BUCKET_BOUNDS]
                labels.append(_WALL_BUCKET_TAIL)
                row.update(zip(labels, buckets, strict=True))
            result[key] = row
        return result

    @property
    def since_ms(self) -> int:
        return self._since_ms
