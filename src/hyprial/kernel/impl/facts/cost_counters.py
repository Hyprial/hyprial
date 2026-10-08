"""Thread-CPU cost counters keyed by a fixed name set.

Lives outside ``hyprial.daemon`` so the Agent actor (``hyprial.agents``) can
own one without importing the daemon package, whose ``__init__`` imports the
application and would make the import circular.  What the counters mean --
the attribution rule and the calibration contract -- is documented once, in
``hyprial.daemon.impl.ipc_stats``.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterable
from typing import Any

__all__ = [
    "OTHER_KEY",
    "CallCostCounters",
    "RUNTIME_OVERFLOW_OWNER",
    "RuntimeCpuCounters",
    "runtime_cpu_counters",
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
#: Distinct error codes kept per key; further codes are counted as "other",
#: so a client that invents codes cannot grow the payload without bound.
_MAX_ERROR_CODES = 16
_OTHER_ERROR_CODE = "other"
_RUNTIME_OWNER_SUFFIX = re.compile(r"-\d+$")
# Per-agent and per-space instances share one owner row: ``turn:<agent>`` and
# ``query-io:<agent>`` keep their kind, and orgfs lanes drop the 12-character
# space-id prefix they embed.  Without this the key budget fills with one row
# per agent/space and is never evicted.
_RUNTIME_OWNER_INSTANCE = re.compile(r":.*$")
_RUNTIME_ORGFS_SPACE = re.compile(
    r"^(orgfs-(?:space|checkout|replica)-).{12}(-[a-z-]+)$"
)
#: Owner of the single reserved row that absorbs work once the key budget is
#: full.  It is never a real owner, so budget alarms skip it.
RUNTIME_OVERFLOW_OWNER = "_overflow"
_RUNTIME_MAX_KEYS = 512
_RUNTIME_EXCLUDED_OWNERS = frozenset(
    {
        # These owners retain the six existing ipcStats sides.  Counting their
        # generic actor/effect choke point as well would charge the same CPU
        # twice and break the calibration upper bound.
        "agent-registry-authority",
        "desired-state-io",
        "session-authority",
        "state-persistence-authority",
    }
)


class _Totals:
    __slots__ = ("calls", "errors", "cpu", "wall", "max_wall", "buckets", "error_codes")

    def __init__(self) -> None:
        self.calls = 0
        self.errors = 0
        self.error_codes: dict[str, int] = {}
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
        error_code: str | None = None,
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
                if error_code:
                    codes = totals.error_codes
                    code = (
                        error_code
                        if error_code in codes or len(codes) < _MAX_ERROR_CODES
                        else _OTHER_ERROR_CODE
                    )
                    codes[code] = codes.get(code, 0) + 1
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
                    dict(totals.error_codes),
                )
                for key, totals in self._totals.items()
            ]
        result: dict[str, dict[str, Any]] = {}
        for key, calls, errors, cpu, wall, max_wall, buckets, codes in sorted(items):
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
            if codes:
                # Additive: which refusals the error count is made of (a
                # 1 ms fence rejection and an overload look alike otherwise).
                row["errorCodes"] = dict(sorted(codes.items()))
            result[key] = row
        return result

    @property
    def since_ms(self) -> int:
        return self._since_ms


class _RuntimeTotals:
    __slots__ = ("calls", "cpu")

    def __init__(self) -> None:
        self.calls = 0
        self.cpu = 0.0


class RuntimeCpuCounters:
    """Bounded per-owner/per-work thread-CPU accounting.

    Actor and effect worker threads normalize their declared owner once, then
    record one completed unit here.  One slot is reserved: once the other
    ``max_keys - 1`` keys exist, every unseen key folds into
    ``_overflow/_other``, never into another owner's row, so attribution stays
    honest and the map never exceeds ``max_keys``.
    """

    def __init__(
        self,
        *,
        max_keys: int = _RUNTIME_MAX_KEYS,
        excluded_owners: Iterable[str] = (),
    ) -> None:
        if max_keys < 2:
            raise ValueError("runtime CPU key limit must be positive")
        self._max_keys = max_keys
        self._excluded = frozenset(
            self.normalize_owner(owner) for owner in excluded_owners
        )
        self._lock = threading.Lock()
        self._totals: dict[str, _RuntimeTotals] = {}
        self._overflow_folds = 0
        self.enabled = True

    @staticmethod
    def normalize_owner(name: str) -> str:
        """Drop worker-instance suffixes and per-agent/per-space identities."""

        owner = _RUNTIME_OWNER_SUFFIX.sub("", name.strip())
        owner = _RUNTIME_OWNER_INSTANCE.sub("", owner)
        owner = _RUNTIME_ORGFS_SPACE.sub(r"\1*\2", owner)
        return owner or "_other"

    def owner(self, declared_name: str) -> str | None:
        """Return the normalized owner, or ``None`` for an existing CPU side."""

        owner = self.normalize_owner(declared_name)
        return None if owner in self._excluded else owner

    def record(
        self, owner: str, work_type: str, *, cpu_seconds: float
    ) -> None:
        normalized = self.owner(owner)
        if normalized is not None:
            self.record_normalized(
                normalized, work_type, cpu_seconds=cpu_seconds
            )

    def record_normalized(
        self, owner: str, work_type: str, *, cpu_seconds: float
    ) -> None:
        """Record one unit for an owner returned by :meth:`owner`."""

        desired = f"{owner}/{work_type}"
        cpu = cpu_seconds if cpu_seconds > 0.0 else 0.0
        with self._lock:
            key = desired
            if key not in self._totals and len(self._totals) >= self._max_keys - 1:
                key = f"{RUNTIME_OVERFLOW_OWNER}/_other"
                self._overflow_folds += 1
            totals = self._totals.get(key)
            if totals is None:
                totals = self._totals[key] = _RuntimeTotals()
            totals.calls += 1
            totals.cpu += cpu

    def snapshot(self) -> dict[str, dict[str, int | float]]:
        with self._lock:
            items = [
                (key, totals.calls, totals.cpu)
                for key, totals in self._totals.items()
            ]
        return {
            key: {"calls": calls, "threadCpuMs": round(cpu * 1000, 3)}
            for key, calls, cpu in sorted(items)
        }

    @property
    def overflow_folds(self) -> int:
        with self._lock:
            return self._overflow_folds


# One daemon process owns one aggregate.  Tests may inject an isolated instance
# into ActorRuntime/EffectLane; production uses this singleton at both choke
# points and in the ps projection.
runtime_cpu_counters = RuntimeCpuCounters(
    excluded_owners=_RUNTIME_EXCLUDED_OWNERS
)
