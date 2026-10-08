"""HookBus: fan observe-only hook events out to isolated, bounded lanes.

Each registered observer owns one *lane*: a one-worker :class:`EffectLane`
that runs ``observer.observe`` in publish order.  Observer code never runs in
an actor mailbox, and a slow or wedged observer only fills its own lane.
``publish`` is one non-blocking ``submit`` per subscribed lane, so no observer
can delay the caller (a delivery pump).

A lane has no owner actor on purpose: completion is settled directly on the
effect worker.  An owner actor would make settlement depend on an actor the
bus does not control; when a shared runtime was drained, accepted events were
stranded uncounted and ``close()`` could never finish.

Delivery guarantee: at-most-once, best-effort, bounded, counted.  Every
published event ends up in exactly one of ``overloaded`` (lane full),
``closed`` (lane closing) or ``accepted``; accepted events end up
``delivered`` or ``failed`` once the observer returns.  Ordering is per-lane
FIFO in publish order; there is no global order across sources, and sources
stamp their own ``seq``.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.kernel.impl.actor_runtime.effects import EffectCompleted, EffectLane, EffectRequest

from hyprial.kernel.impl.hooks.events  import EVENT_NAMES, HookEvent

DEFAULT_LANE_QUEUE = 256
LANE_OVERFLOW_LOG_INTERVAL_SECONDS = 60.0
_LANE_GENERATION = 1


class ObserverHook(Protocol):
    """Observe one event.  Runs on the lane's effect worker, never a mailbox."""

    def observe(self, event: HookEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class LaneProjection:
    name: str
    events: tuple[str, ...]
    accepted: int
    overloaded: int
    closed: int
    delivered: int
    failed: int
    shut: bool


class _Lane:
    def __init__(
        self,
        *,
        name: str,
        observer: ObserverHook,
        events: frozenset[str],
        queue: int,
        logger: Callable[..., None] | None,
        monotonic: Callable[[], float],
    ) -> None:
        self.name = name
        self.events = events
        self._observer = observer
        self._logger = logger
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._shut = False
        self._accepted = self._overloaded = self._closed = 0
        self._delivered = self._failed = 0
        self._overflow_logged_at: float | None = None
        self._effects: EffectLane[HookEvent, None] = EffectLane(
            name=f"hook-lane-{name}",
            execute=self._observer.observe,
            complete=self._settle,
            capacity=queue,
            workers=1,  # one worker keeps observe() calls in publish order
        )

    def offer(self, event: HookEvent) -> AdmissionResult:
        # Runs on the publisher's thread.  submit() never blocks (bounded
        # reserve + put_nowait), so holding the lane lock across it is cheap,
        # and it guarantees `accepted` is counted before the worker's _settle
        # can count `delivered`.  Lock order everywhere: lane lock, then the
        # EffectLane's own condition.
        with self._lock:
            admitted = self._effects.submit(
                EffectRequest(uuid.uuid4().hex, _LANE_GENERATION, event)
            )
            if admitted is AdmissionResult.ACCEPTED:
                self._accepted += 1
                return admitted
            if admitted is AdmissionResult.CLOSED:
                # Refused during shutdown: counted, but not an overflow.
                self._closed += 1
                return admitted
            self._overloaded += 1
            now = self._monotonic()
            previous = self._overflow_logged_at
            should_log = (
                previous is None or now - previous >= LANE_OVERFLOW_LOG_INTERVAL_SECONDS
            )
            if should_log:
                self._overflow_logged_at = now
            dropped = self._overloaded
        if should_log and self._logger is not None:
            self._logger(
                "warn",
                "daemon",
                "hooks.lane_overflow",
                lane=self.name,
                event=event.event,
                dropped=dropped,
            )
        return admitted

    def projection(self) -> LaneProjection:
        with self._lock:
            return LaneProjection(
                self.name,
                tuple(sorted(self.events)),
                self._accepted,
                self._overloaded,
                self._closed,
                self._delivered,
                self._failed,
                self._shut,
            )

    def close(self, deadline: float) -> bool:
        # Stops admission, then waits for the worker to finish everything it
        # already accepted.  A wedged observer makes this return False; its
        # stranded events stay visible as accepted minus delivered/failed.
        shut = self._effects.close(max(0.0, deadline - time.monotonic()))
        with self._lock:
            self._shut = shut
        return shut

    def _settle(self, completed: EffectCompleted[None]) -> AdmissionResult:
        # Runs on the effect worker after observe() returned or raised, outside
        # the EffectLane's condition; acknowledge() then takes that condition,
        # keeping the lane-lock-first order.
        with self._lock:
            if completed.error is None:
                self._delivered += 1
            else:
                self._failed += 1
        self._effects.acknowledge(completed.operation_id, completed.generation)
        return AdmissionResult.ACCEPTED


class HookBus:
    """Observe-only hook fan-out.  Default off: no lanes, no work."""

    def __init__(
        self,
        *,
        logger: Callable[..., None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._closed = False
        self._lanes: dict[str, _Lane] = {}
        # Copy-on-write so publish reads a stable snapshot without the lock.
        self._routes: Mapping[str, tuple[_Lane, ...]] = MappingProxyType({})

    def register(
        self,
        name: str,
        observer: ObserverHook,
        events: Iterable[str],
        *,
        queue: int = DEFAULT_LANE_QUEUE,
    ) -> None:
        subscribed = frozenset(events)
        if not name.strip():
            raise ValueError("hook lane name must not be blank")
        if not subscribed:
            raise ValueError("hook lane must subscribe to at least one event")
        unknown = subscribed - EVENT_NAMES
        if unknown:
            raise ValueError(f"unknown hook events: {sorted(unknown)!r}")
        if queue < 1:
            raise ValueError("hook lane queue must be at least 1")
        with self._lock:
            if self._closed:
                raise RuntimeError("hook bus is closed")
            if name in self._lanes:
                raise ValueError(f"hook lane {name!r} is already registered")
            lane = _Lane(
                name=name,
                observer=observer,
                events=subscribed,
                queue=queue,
                logger=self._logger,
                monotonic=self._monotonic,
            )
            self._lanes[name] = lane
            routes = {key: list(value) for key, value in self._routes.items()}
            for event_name in subscribed:
                routes.setdefault(event_name, []).append(lane)
            self._routes = MappingProxyType(
                {key: tuple(value) for key, value in routes.items()}
            )

    def publish(self, event: HookEvent) -> int:
        """Offer ``event`` to every subscribed lane; return how many accepted.

        Never blocks on an observer.  Intercept events belong to the intercept
        port (a later slice) and are refused here.
        """

        if event.kind != "observe":
            raise ValueError("HookBus.publish carries observe events only")
        lanes = self._routes.get(event.event, ())
        return sum(
            lane.offer(event) is AdmissionResult.ACCEPTED for lane in lanes
        )

    def projection(self) -> tuple[LaneProjection, ...]:
        with self._lock:
            lanes = tuple(self._lanes.values())
        return tuple(lane.projection() for lane in lanes)

    def close(self, timeout: float = 5.0) -> bool:
        """Stop admission and wait for accepted events; False if any lane is wedged.

        Idempotent: a later call keeps waiting on lanes that did not finish.
        """

        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            self._closed = True
            self._routes = MappingProxyType({})
            lanes = tuple(self._lanes.values())
        return all([lane.close(deadline) for lane in lanes])


__all__ = [
    "DEFAULT_LANE_QUEUE",
    "LANE_OVERFLOW_LOG_INTERVAL_SECONDS",
    "HookBus",
    "LaneProjection",
    "ObserverHook",
]
