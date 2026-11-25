# Decision record (file header; no inline comments by repository rule):
# - A worker pool is one logical actor with a custody token per
#   (operation_id, generation), executed by the shipped EffectLane. The lane
#   already owns the work queue and the retry-until-acknowledged delivery, so
#   this record adds only owner routing, pool lifecycle, and the one
#   child_stopped event the lane cannot emit itself; it is not a second
#   mailbox.
# - Completion is internal admission to the owner (the ABI 9.4 rule that drain
#   never refuses completion custody). A closed admission whose owner is
#   stopped or quarantined becomes OwnerGone and is dead-lettered once, which
#   settles the token. A stopping owner keeps the lane retrying.
# - Worker failures become EffectCompleted(error=type name); the pool has no
#   supervision and never restarts a worker.
from __future__ import annotations

import threading
from typing import Any

from hyprial.kernel.impl.actor_runtime.contracts import (
    ActorEvent,
    ActorEventKind,
    ActorHandle,
    ActorSnapshot,
    ActorState,
    AdmissionResult,
)
from hyprial.kernel.impl.actor_runtime.effects import (
    EffectCompleted,
    EffectLane,
    EffectRequest,
    OwnerGone,
)
from hyprial.kernel.impl.actor_runtime.port.contracts import ActorPoolSpec


class WorkerPoolRecord:
    """One pool actor's lane, owner table, and lifecycle."""

    kind = "pool"

    def __init__(
        self, engine: Any, handle: ActorHandle, spec: ActorPoolSpec
    ) -> None:
        self._engine = engine
        self.handle = handle
        self.spec = spec
        self._spare = [spec.handler_factory() for _ in range(spec.pool_size)]
        self._local = threading.local()
        self._lock = threading.Lock()
        self._owners: dict[tuple[str, int], ActorHandle] = {}
        self._in_flight = 0
        self._stopped_emitted = False
        self._lane: EffectLane = EffectLane(
            name=f"actor-pool-{spec.name}",
            execute=self._execute,
            complete=self._complete,
            capacity=spec.capacity,
            workers=spec.pool_size,
            retry_seconds=spec.retry_interval,
            dead_letter_sink=self._dead_letter,
        )

    def _execute(self, payload: object) -> object:
        handler = getattr(self._local, "handler", None)
        if handler is None:
            with self._lock:
                handler = self._spare.pop()
            self._local.handler = handler
        with self._lock:
            self._in_flight += 1
        try:
            return handler(payload)
        finally:
            with self._lock:
                self._in_flight -= 1

    def _complete(
        self, completion: EffectCompleted
    ) -> AdmissionResult | OwnerGone:
        with self._lock:
            owner = self._owners.get(
                (completion.operation_id, completion.generation)
            )
        if owner is None:
            return AdmissionResult.CLOSED
        admission = self._engine._tell_internal(owner, completion)
        if admission is not AdmissionResult.CLOSED:
            return admission
        try:
            snapshot = self._engine._snapshot(owner)
        except KeyError:
            return AdmissionResult.CLOSED
        if snapshot.state in (ActorState.STOPPED, ActorState.QUARANTINED):
            return OwnerGone(snapshot.state.value)
        return admission

    def _dead_letter(self, completion: EffectCompleted, reason: str) -> None:
        with self._lock:
            self._owners.pop(
                (completion.operation_id, completion.generation), None
            )
        sink = self.spec.undelivered_sink
        if sink is None:
            return
        try:
            sink(completion, reason)
        except BaseException:
            pass

    def reserve(self, operation_id: str, generation: int) -> AdmissionResult:
        return self._lane.reserve(operation_id, generation)

    def submit(
        self,
        owner: ActorHandle,
        operation_id: str,
        generation: int,
        payload: object,
    ) -> AdmissionResult:
        key = (operation_id, generation)
        with self._lock:
            inserted = key not in self._owners
            if inserted:
                self._owners[key] = owner
        outcome = self._lane.submit(
            EffectRequest(operation_id, generation, payload)
        )
        if outcome is not AdmissionResult.ACCEPTED and inserted:
            with self._lock:
                self._owners.pop(key, None)
        return outcome

    def submit_reserved(
        self,
        owner: ActorHandle,
        operation_id: str,
        generation: int,
        payload: object,
    ) -> AdmissionResult:
        key = (operation_id, generation)
        with self._lock:
            inserted = key not in self._owners
            if inserted:
                self._owners[key] = owner
        outcome = self._lane.submit_reserved(
            EffectRequest(operation_id, generation, payload)
        )
        if outcome is not AdmissionResult.ACCEPTED and inserted:
            with self._lock:
                self._owners.pop(key, None)
        return outcome

    def cancel(self, operation_id: str, generation: int) -> bool:
        cancelled = self._lane.cancel_reservation(operation_id, generation)
        if cancelled:
            with self._lock:
                self._owners.pop((operation_id, generation), None)
        return cancelled

    def acknowledge(self, operation_id: str, generation: int) -> bool:
        settled = self._lane.acknowledge(operation_id, generation)
        if settled:
            with self._lock:
                self._owners.pop((operation_id, generation), None)
        return settled

    def _alive(self) -> bool:
        return any(thread.is_alive() for thread in self._lane._threads)

    def state(self) -> ActorState:
        if not self._lane.snapshot().closed:
            return ActorState.RUNNING
        return ActorState.STOPPING if self._alive() else ActorState.STOPPED

    def snapshot(self) -> ActorSnapshot:
        with self._lane._condition:
            queued = len(self._lane._requests)
        with self._lock:
            in_flight = self._in_flight
        return ActorSnapshot(
            handle=self.handle,
            state=self.state(),
            generation=1,
            queued=queued,
            in_flight=in_flight,
            failures_in_window=0,
        )

    def idle(self) -> bool:
        if not self._alive():
            return True
        with self._lane._condition:
            queued = len(self._lane._requests)
        with self._lock:
            return queued == 0 and self._in_flight == 0 and not self._owners

    def stop(self, timeout: float) -> bool:
        stopped = self._lane.close(timeout)
        if stopped:
            self._emit_stopped()
        return stopped

    def close_admission(self) -> None:
        self._lane.close(0.0)

    def _emit_stopped(self) -> None:
        with self._lock:
            if self._stopped_emitted:
                return
            self._stopped_emitted = True
        self._engine._emit_for_spec(
            self.spec,
            ActorEvent(
                kind=ActorEventKind.CHILD_STOPPED,
                handle=self.handle,
                generation=1,
            ),
        )
