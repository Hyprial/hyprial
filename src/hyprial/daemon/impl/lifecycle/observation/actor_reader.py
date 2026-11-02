"""Refresh an aggregate actor's terminal fact at its observation read boundary."""

from typing import Protocol

from hyprial.kernel import (
    ActorHandle, ActorState, LifecycleSnapshot, LifecycleTarget,
    ObservationBatch, ObservationSubscription,
)
from .ports import LifecycleObservationOwner, LifecycleObservationReader


class _StateSnapshot(Protocol):
    @property
    def state(self) -> ActorState: ...


class _RuntimeReadPort(Protocol):
    def snapshot(self, handle: ActorHandle) -> _StateSnapshot: ...


class ActorLifecycleObservationReader(LifecycleObservationReader):
    """Read existing runtime state; never install a callback or send a command."""

    def __init__(self, owner: LifecycleObservationOwner, runtime: _RuntimeReadPort,
                 handle: ActorHandle, source_key: str, source_epoch: str) -> None:
        super().__init__(owner._projection)
        self._owner = owner
        self._runtime = runtime
        self._handle = handle
        self._source_key = source_key
        self._source_epoch = source_epoch

    def _refresh(self) -> None:
        if self._runtime.snapshot(self._handle).state is ActorState.QUARANTINED:
            self._owner.publish(self._source_key, self._source_epoch,
                                state="down", cause="coordinator-failed", available=False)

    def snapshot(self, target: LifecycleTarget, *, namespace: str = "lifecycle") -> LifecycleSnapshot:
        self._refresh()
        return super().snapshot(target, namespace=namespace)

    def observe(
        self, target: LifecycleTarget, *, namespace: str = "lifecycle",
        after_revision: int | None = None, epoch: str | None = None,
        limit: int = 128, wait_ms: int = 0, observer_id: str | None = None,
    ) -> ObservationBatch:
        self._refresh()
        return super().observe(target, namespace=namespace, after_revision=after_revision,
                               epoch=epoch, limit=limit, wait_ms=wait_ms, observer_id=observer_id)

    def open(self, target: LifecycleTarget, *, namespace: str = "lifecycle") -> ObservationSubscription:
        self._refresh()
        return super().open(target, namespace=namespace)
