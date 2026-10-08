"""Windowed operator alarms for the state writer and runtime CPU owners.

Production 0.5.0, 2026-10-05: every desired-state write cost ~0.5 s and the
writer was busy essentially all the time for hours; session calls timed out
and Channels stopped waking, and nothing said so.

The alarm judges *utilization*: the writer's execution time divided by the
elapsed time over a window of at least ``STATE_WRITER_WINDOW_SECONDS``,
accumulated across maintenance ticks.  An average-latency-per-tick rule
cannot fire on exactly this shape -- a serial writer at 500 ms per command
completes ~2 commands in a 1 s tick, and fast reads dilute the average --
and the mailbox stays shallow because callers time out (review 821 on
#1145, reproduced against the real authority).

The runtime alarm applies the same window/de-duplication discipline to the
generic actor/effect/maintenance CPU counters and reports the top work types
for each owner that exceeds its configured share of one core.
"""

from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

from hyprial.kernel import RUNTIME_OVERFLOW_OWNER

#: Shortest window a utilization is judged over.
STATE_WRITER_WINDOW_SECONDS = 30.0
#: Share of the window the writer spent executing that counts as saturated.
STATE_WRITER_SATURATED_UTILIZATION = 0.8
#: Utilization below which a raised alarm is reported as recovered.
STATE_WRITER_RECOVERED_UTILIZATION = 0.5
#: Mailbox depth that counts as backed up (the mailbox holds 128).
STATE_WRITER_QUEUED_LIMIT = 32
#: Minimum gap between two alarms while the writer stays saturated.
STATE_WRITER_ALARM_COOLDOWN_SECONDS = 300.0

# Runtime CPU owners are judged against one logical core.  The environment
# override is intentionally a share rather than a machine-wide percentage:
# actor and effect lanes each execute on one thread.
CPU_OWNER_WINDOW_SECONDS = 30.0
CPU_OWNER_DEFAULT_SHARE = 0.5
CPU_OWNER_BUDGET_SHARE_ENV = "HYPRIAL_CPU_OWNER_BUDGET_SHARE"
#: Minimum gap between two alarms for the same owner.  A window-long gap
#: would repeat a sustained hot owner ~2880 times a day (review 912).
CPU_OWNER_ALARM_COOLDOWN_SECONDS = 300.0


def cpu_owner_budget_share(
    environ: Mapping[str, str] = os.environ,
) -> float:
    raw = environ.get(CPU_OWNER_BUDGET_SHARE_ENV, "").strip()
    if not raw:
        return CPU_OWNER_DEFAULT_SHARE
    try:
        share = float(raw)
    except ValueError:
        return CPU_OWNER_DEFAULT_SHARE
    return share if share > 0.0 else CPU_OWNER_DEFAULT_SHARE


def _totals(status: Mapping[str, Any]) -> tuple[int, float, dict[str, tuple[int, float]]]:
    rows = {
        key: (int(row.get("calls", 0)), float(row.get("wallMs", 0.0)))
        for key, row in status.get("execution", {}).items()
    }
    return (
        sum(calls for calls, _ in rows.values()),
        sum(wall for _, wall in rows.values()),
        rows,
    )


class StateWriterAlarm:
    def __init__(
        self,
        *,
        window_seconds: float = STATE_WRITER_WINDOW_SECONDS,
        saturated: float = STATE_WRITER_SATURATED_UTILIZATION,
        recovered: float = STATE_WRITER_RECOVERED_UTILIZATION,
        queued_limit: int = STATE_WRITER_QUEUED_LIMIT,
        cooldown_seconds: float = STATE_WRITER_ALARM_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_seconds = window_seconds
        self._saturated = saturated
        self._recovered = recovered
        self._queued_limit = queued_limit
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        # (observed_at, total calls, total busy ms, per-key totals)
        self._samples: deque[tuple[float, int, float, dict[str, tuple[int, float]]]] = deque()
        self._last_alarm: float | None = None
        self._alarmed = False

    def observe(self, status: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        """Return ``("slow"|"recovered", fields)`` for this tick, or None."""

        now = self._clock()
        calls, busy_ms, rows = _totals(status)
        queued = int(status.get("mailbox", {}).get("queued", 0))
        self._samples.append((now, calls, busy_ms, rows))
        # Keep the newest sample that is at least one window old, drop older.
        while len(self._samples) > 1 and now - self._samples[1][0] >= self._window_seconds:
            self._samples.popleft()
        then, then_calls, then_busy, then_rows = self._samples[0]
        elapsed = now - then
        if elapsed < self._window_seconds:
            fields = None
            utilization = None
        else:
            utilization = (busy_ms - then_busy) / (elapsed * 1000.0)
            fields = self._fields(
                utilization, elapsed, calls - then_calls, busy_ms - then_busy,
                queued, rows, then_rows,
            )
        backed_up = queued >= self._queued_limit
        saturated = utilization is not None and utilization >= self._saturated
        if saturated or backed_up:
            alarm_fields = fields or {
                "queued": queued,
                "windowMs": round(elapsed * 1000),
            }
            if (
                self._last_alarm is not None
                and now - self._last_alarm < self._cooldown_seconds
            ):
                if self._alarmed:
                    return None
                self._alarmed = True
                return "slow", {**alarm_fields, "_page": False}
            self._last_alarm = now
            self._alarmed = True
            return "slow", alarm_fields
        if (
            self._alarmed
            and utilization is not None
            and utilization < self._recovered
            and queued < self._queued_limit // 2
        ):
            # Keep _last_alarm: recovery does not reset the cooldown, or a
            # writer flapping around the threshold pages on every swing
            # (review 822: 40/h on a square wave, up to 1785/h on the queue).
            self._alarmed = False
            return "recovered", fields or {}
        return None

    @staticmethod
    def _fields(
        utilization: float,
        elapsed: float,
        commands: int,
        busy_ms: float,
        queued: int,
        rows: dict[str, tuple[int, float]],
        then_rows: dict[str, tuple[int, float]],
    ) -> dict[str, Any]:
        worst: tuple[float, str] | None = None
        for key, (key_calls, key_wall) in rows.items():
            before_calls, before_wall = then_rows.get(key, (0, 0.0))
            delta_calls = key_calls - before_calls
            if delta_calls <= 0:
                continue
            share = key_wall - before_wall
            if worst is None or share > worst[0]:
                worst = (share, key)
        return {
            "utilization": round(utilization, 3),
            "windowMs": round(elapsed * 1000),
            "busyMs": round(busy_ms, 1),
            "commands": commands,
            "averageExecutionMs": round(busy_ms / commands, 1) if commands else None,
            "queued": queued,
            "busiestCommand": None if worst is None else worst[1],
            "busiestCommandMs": None if worst is None else round(worst[0], 1),
        }


class CpuOwnerBudgetAlarm:
    """Emit an over-budget event per owner, at most once per cooldown.

    The reserved overflow row is not an owner and never alarms.
    """

    def __init__(
        self,
        *,
        window_seconds: float = CPU_OWNER_WINDOW_SECONDS,
        share: float | None = None,
        cooldown_seconds: float = CPU_OWNER_ALARM_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if window_seconds < CPU_OWNER_WINDOW_SECONDS:
            raise ValueError("CPU owner alarm window must be at least 30 seconds")
        resolved_share = cpu_owner_budget_share() if share is None else share
        if resolved_share <= 0.0:
            raise ValueError("CPU owner budget share must be positive")
        self._window_seconds = window_seconds
        self._cooldown_seconds = max(cooldown_seconds, window_seconds)
        self._share = resolved_share
        self._clock = clock
        self._samples: deque[
            tuple[float, dict[str, tuple[int, float]]]
        ] = deque()
        self._last_alarm: dict[str, float] = {}

    def observe(
        self, runtime: Mapping[str, Mapping[str, Any]]
    ) -> tuple[dict[str, Any], ...]:
        now = self._clock()
        rows = {
            key: (
                int(row.get("calls", 0)),
                float(row.get("threadCpuMs", 0.0)),
            )
            for key, row in runtime.items()
        }
        self._samples.append((now, rows))
        while (
            len(self._samples) > 1
            and now - self._samples[1][0] >= self._window_seconds
        ):
            self._samples.popleft()
        then, previous = self._samples[0]
        elapsed = now - then
        if elapsed < self._window_seconds:
            return ()

        owners: dict[str, list[tuple[str, int, float]]] = {}
        for key, (calls, cpu_ms) in rows.items():
            owner, separator, work_type = key.partition("/")
            if not separator or owner == RUNTIME_OVERFLOW_OWNER:
                continue
            before_calls, before_cpu_ms = previous.get(key, (0, 0.0))
            delta_calls = calls - before_calls
            delta_cpu_ms = cpu_ms - before_cpu_ms
            if delta_calls <= 0 or delta_cpu_ms < 0.0:
                continue
            owners.setdefault(owner, []).append(
                (work_type, delta_calls, delta_cpu_ms)
            )

        events: list[dict[str, Any]] = []
        for owner, work in sorted(owners.items()):
            cpu_ms = sum(item[2] for item in work)
            used_share = cpu_ms / (elapsed * 1000.0)
            if used_share <= self._share:
                continue
            last = self._last_alarm.get(owner)
            if last is not None and now - last < self._cooldown_seconds:
                continue
            self._last_alarm[owner] = now
            top = sorted(work, key=lambda item: item[2], reverse=True)[:3]
            events.append(
                {
                    "owner": owner,
                    "windowMs": round(elapsed * 1000),
                    "threadCpuMs": round(cpu_ms, 3),
                    "share": round(used_share, 3),
                    "topWorkTypes": [
                        {
                            "workType": work_type,
                            "calls": calls,
                            "threadCpuMs": round(work_cpu_ms, 3),
                        }
                        for work_type, calls, work_cpu_ms in top
                    ],
                }
            )
        return tuple(events)
