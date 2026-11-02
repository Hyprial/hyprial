"""Bounded diagnostic history at one lock-protected publication boundary.

No work queue, native process handles, callbacks, or waiting threads live here.
Eviction loses diagnostics only. A missing resource is never evidence of exit.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from uuid import uuid4

from hyprial.kernel import (
    LifecycleCursor, LifecycleEvent, LifecycleObservationState, LifecycleSnapshot,
    LifecycleTarget, ObservationBatch, ObservationSubscription,
)


@dataclass(slots=True)
class _Entry:
    current: LifecycleSnapshot
    events: deque[LifecycleEvent]
    source_epoch: str | None = None
    observers: set[str] = field(default_factory=set)


def _namespace(namespace: str) -> None:
    if namespace != "lifecycle":
        raise ValueError("only the lifecycle namespace is supported")


class LifecycleObservationProjection:
    """Owner-side publication; consumers receive a separate read-only facade.

    Limits cover all memory, including absent keys and subscriptions. Oldest
    targets are evicted without pinning by readers. Eviction closes their
    subscriptions; subsequent polls must resync. Epochs must not be reused by
    a publisher for a new binding. Read calls never touch owner business state.
    """

    def __init__(self, *, history_limit: int = 128, observer_limit: int = 64,
                 target_limit: int = 4096) -> None:
        if min(history_limit, observer_limit, target_limit) < 1:
            raise ValueError("observation capacities must be positive")
        self._history_limit = history_limit
        self._observer_limit = observer_limit
        self._target_limit = target_limit
        self._entries: OrderedDict[LifecycleTarget, _Entry] = OrderedDict()
        self._lock = threading.RLock()
        self._closed = False
        self._absence_epoch = uuid4().hex

    def _current(self, target: LifecycleTarget) -> LifecycleSnapshot:
        entry = self._entries.get(target)
        if entry is not None:
            return entry.current
        return LifecycleSnapshot(target, self._absence_epoch, 0, time.time_ns() // 1_000_000,
                                 "unknown", "noproc", False)

    def _install(self, target: LifecycleTarget, snapshot: LifecycleSnapshot) -> _Entry:
        if len(self._entries) >= self._target_limit:
            self._entries.popitem(last=False)
            self._absence_epoch = uuid4().hex
        entry = _Entry(snapshot, deque(maxlen=self._history_limit))
        self._entries[target] = entry
        return entry

    def publish(self, target: LifecycleTarget, *, epoch: str,
                state: LifecycleObservationState, cause: str, available: bool,
                observed_at_ms: int | None = None) -> LifecycleSnapshot:
        if not epoch or not cause or state not in {
            "unknown", "starting", "running", "stopping", "stopped", "down",
        }:
            raise ValueError("invalid lifecycle observation fact")
        with self._lock:
            if self._closed:
                return self._current(target)
            entry = self._entries.get(target)
            current = None if entry is None else entry.current
            same_epoch = entry is not None and entry.source_epoch == epoch
            if same_epoch and (current.state, current.cause, current.available) == (state, cause, available):
                return current
            revision = current.revision + 1 if same_epoch else 1
            diagnostic_epoch = current.epoch if same_epoch else uuid4().hex
            snapshot = LifecycleSnapshot(
                target, diagnostic_epoch, revision,
                time.time_ns() // 1_000_000 if observed_at_ms is None else observed_at_ms,
                state, cause, available,
            )
            if entry is None:
                entry = self._install(target, snapshot)
            else:
                if not same_epoch:
                    entry.events.clear()
                entry.current = snapshot
            entry.source_epoch = epoch
            entry.events.append(LifecycleEvent(target, diagnostic_epoch, revision, state, cause, snapshot.observed_at_ms))
            return snapshot

    def snapshot(self, target: LifecycleTarget, *, namespace: str = "lifecycle") -> LifecycleSnapshot:
        _namespace(namespace)
        with self._lock:
            return self._current(target)

    def observe(
        self, target: LifecycleTarget, *, namespace: str = "lifecycle",
        after_revision: int | None = None, epoch: str | None = None,
        limit: int = 128, wait_ms: int = 0, observer_id: str | None = None,
    ) -> ObservationBatch:
        _namespace(namespace)
        if wait_ms != 0 or not 1 <= limit <= 128:
            raise ValueError("observation requires wait_ms=0 and 1 <= limit <= 128")
        with self._lock:
            current = self._current(target)
            entry = self._entries.get(target)
            closed = self._closed or (observer_id is not None and (
                entry is None or observer_id not in entry.observers
            ))
            cursor = LifecycleCursor(current.epoch, current.revision)
            if closed:
                return ObservationBatch((), cursor, False, current, "closed")
            first = after_revision is None and epoch is None
            resync = not first and (
                after_revision is None or epoch != current.epoch
                or after_revision < 0 or after_revision > current.revision
                or (entry is not None and bool(entry.events)
                    and after_revision < entry.events[0].revision - 1)
            )
            events = ()
            if not first and not resync and entry is not None:
                events = tuple(event for event in entry.events if event.revision > after_revision)[:limit]
                if events:
                    cursor = LifecycleCursor(current.epoch, events[-1].revision)
            return ObservationBatch(events, cursor, resync, current)

    def open(self, target: LifecycleTarget, *, namespace: str = "lifecycle") -> ObservationSubscription:
        _namespace(namespace)
        with self._lock:
            initial = self.observe(target)
            if self._closed:
                return ObservationSubscription("closed", None, initial)
            entry = self._entries.get(target)
            if entry is None:
                entry = self._install(target, initial.current)
            if len(entry.observers) >= self._observer_limit:
                return ObservationSubscription("overloaded", None, initial)
            observer_id = uuid4().hex
            entry.observers.add(observer_id)
            return ObservationSubscription("ok", observer_id, initial)

    def cancel(self, observer_id: str) -> None:
        with self._lock:
            for entry in self._entries.values():
                if observer_id in entry.observers:
                    entry.observers.remove(observer_id)
                    return

    def forget(self, target: LifecycleTarget) -> None:
        """Release only diagnostic resources when a mount is removed."""
        with self._lock:
            if self._entries.pop(target, None) is not None:
                self._absence_epoch = uuid4().hex

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._entries.clear()
