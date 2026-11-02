"""Owner-side fact adapter and read-only monitor facade.

Composition binds a canonical incarnation target to an existing owner's source
identity. This is a diagnostic association, not process custody or an agent mount.
The cell mount package supplies those associations; this module never resolves
identity, reads credentials, executes a process, or creates a business resource.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

from hyprial.kernel import (
    LifecycleObservationState, LifecycleSnapshot, LifecycleTarget,
    ObservationBatch, ObservationSubscription,
)
from .projection import LifecycleObservationProjection


class LifecycleObservationReader:
    """Only observation and observer cancellation cross the consumer port."""

    def __init__(self, projection: LifecycleObservationProjection) -> None:
        self.__projection = projection

    def snapshot(self, target: LifecycleTarget, *, namespace: str = "lifecycle") -> LifecycleSnapshot:
        return self.__projection.snapshot(target, namespace=namespace)

    def observe(
        self, target: LifecycleTarget, *, namespace: str = "lifecycle",
        after_revision: int | None = None, epoch: str | None = None,
        limit: int = 128, wait_ms: int = 0, observer_id: str | None = None,
    ) -> ObservationBatch:
        return self.__projection.observe(target, namespace=namespace, after_revision=after_revision,
                                         epoch=epoch, limit=limit, wait_ms=wait_ms, observer_id=observer_id)

    def open(self, target: LifecycleTarget, *, namespace: str = "lifecycle") -> ObservationSubscription:
        return self.__projection.open(target, namespace=namespace)

    def cancel(self, observer_id: str) -> None:
        self.__projection.cancel(observer_id)


@dataclass(frozen=True, slots=True)
class _Fact:
    source_epoch: str
    runtime_epoch: str
    state: LifecycleObservationState
    cause: str
    available: bool


class LifecycleObservationOwner:
    """Bounded projection hosted at an existing serialized owner's boundary.

    publish must follow source-owner ordering and generation fencing. bind and
    publish share this lock, so an exit concurrent with installation is either
    the initial snapshot or a subsequent retained transition. Source identities
    remain private; observation epochs are freshly minted diagnostic IDs.
    """

    def __init__(self, *, capacity: int = 4096) -> None:
        self._projection = LifecycleObservationProjection(target_limit=capacity)
        self.reader = LifecycleObservationReader(self._projection)
        self._lock = threading.RLock()
        self._capacity = capacity
        self._facts: OrderedDict[str, _Fact] = OrderedDict()
        self._bindings: OrderedDict[LifecycleTarget, tuple[str, str]] = OrderedDict()
        self._closed = False

    def bind(self, target: LifecycleTarget, source_key: str, source_epoch: str) -> None:
        if not source_key or not source_epoch:
            raise ValueError("source key and epoch must be nonempty")
        with self._lock:
            if self._closed:
                return
            binding = (source_key, source_epoch)
            prior = self._bindings.get(target)
            if prior is not None and prior != binding:
                self._projection.forget(target)
            if prior is None and len(self._bindings) >= self._capacity:
                evicted, _ = self._bindings.popitem(last=False)
                self._projection.forget(evicted)
            self._bindings[target] = binding
            self._publish_bound(target, source_key, source_epoch)

    def unbind(self, target: LifecycleTarget) -> None:
        with self._lock:
            self._bindings.pop(target, None)
            self._projection.forget(target)

    def publish(self, source_key: str, source_epoch: str, *, state: LifecycleObservationState,
                cause: str, available: bool, runtime_epoch: str | None = None) -> None:
        with self._lock:
            if self._closed:
                return
            if source_key not in self._facts and len(self._facts) >= self._capacity:
                evicted, _ = self._facts.popitem(last=False)
                for target, (key, epoch) in self._bindings.items():
                    if key == evicted:
                        self._publish_bound(target, key, epoch)
            self._facts[source_key] = _Fact(source_epoch, runtime_epoch or source_epoch,
                                           state, cause, available)
            for target, (key, epoch) in self._bindings.items():
                if key == source_key:
                    self._publish_bound(target, key, epoch)

    def _publish_bound(self, target: LifecycleTarget, key: str, epoch: str) -> None:
        fact = self._facts.get(key)
        if fact is None or fact.source_epoch != epoch:
            self._projection.publish(
                target, epoch=epoch + ":unavailable", state="unknown", available=False,
                cause="source-unavailable" if fact is None else "binding-changed",
            )
        else:
            self._projection.publish(target, epoch=fact.runtime_epoch, state=fact.state,
                                     cause=fact.cause, available=fact.available)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._facts.clear()
            self._bindings.clear()
            self._projection.close()
