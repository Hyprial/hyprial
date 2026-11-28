from __future__ import annotations


from contextlib import contextmanager




import threading

import time

import uuid

import weakref



from hyprial.kernel import (
    ActorSpec,
    AdmissionResult)

from hyprial.kernel import EffectCompleted, EffectRequest


from hyprial.daemon.impl.orgfs.api  import (
    OrgFsError)


from hyprial.daemon.impl.orgfs.document.model import _FACADE_EFFECT_CAPACITY, _FACADE_NETWORK_EFFECT_CAPACITY, _FacadeEffectBatch, _FacadeEffectResult, _MetaWatchNotification, _WatchNotification

class FacadeEffects:
    """Responsibility methods on the sole LocalOrgFs state host.

    This class never constructs, copies, or persists an independent host.
    """

    def _defer_effect(self, effect: object) -> None:
        pending = getattr(self._effect_context, "pending", None)
        if pending is None:
            raise RuntimeError("orgfs effect was produced without a reservation scope")
        pending.append(effect)


    def effect_failure_snapshot(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._effect_failures)


    def _needs_post_commit_effects(self) -> bool:
        with self._lock:
            has_watcher = any(not watcher.closed for watcher in self._watchers.values())
            has_meta_watcher = any(
                not watcher.closed for watcher in self._meta_watchers.values()
            )
        return has_watcher or has_meta_watcher


    def _ensure_effect_lane(self) -> None:
        with self._lock:
            self._ensure_effect_lane_locked()


    def _ensure_effect_lane_locked(self) -> None:
        if self._effect_closed:
            raise OrgFsError("unavailable", {"message": "orgfs is closing"})
        if self._effect_lane is not None:
            return
        owner_ref = weakref.ref(self)

        def handle_completion(command: object) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._on_effect_completion(command)

        def execute_batch(batch: _FacadeEffectBatch) -> _FacadeEffectResult:
            owner = owner_ref()
            return (
                owner._execute_effect_batch(batch)
                if owner is not None
                else _FacadeEffectResult(("owner-closed",))
            )

        def execute_network(space_id: str) -> _FacadeEffectResult:
            owner = owner_ref()
            return (
                owner._execute_network_effect(space_id)
                if owner is not None
                else _FacadeEffectResult(("owner-closed",))
            )

        host = self._actor_hosts.host
        cell = host.spawn(
            ActorSpec(
                "orgfs-post-commit-effects",
                lambda: handle_completion,
                mailbox_capacity=_FACADE_EFFECT_CAPACITY,
                supervision_profile="state_authority",
            )
        )
        self._effect_cell = cell
        self._effect_lane = host.effect_lane(
            name="orgfs-post-commit-callbacks",
            execute=execute_batch,
            complete=lambda completion: cell.tell(completion),
            capacity=_FACADE_EFFECT_CAPACITY,
            workers=1,
        )
        self._network_effect_lane = host.effect_lane(
            name="orgfs-post-commit-network",
            execute=execute_network,
            complete=lambda completion: cell.tell(completion),
            capacity=_FACADE_NETWORK_EFFECT_CAPACITY,
            workers=4,
        )


    def _execute_effect_batch(
        self, batch: _FacadeEffectBatch
    ) -> _FacadeEffectResult:
        failures: list[str] = []
        self._effect_context.in_effect_worker = True
        try:
            for effect in batch.effects:
                try:
                    if isinstance(effect, _WatchNotification):
                        with self._lock:
                            watcher = self._watchers.get(effect.watch_id)
                            callback = (
                                watcher.callback
                                if watcher is not None and not watcher.closed
                                else None
                            )
                        if callback is not None:
                            callback(effect.event)
                    elif isinstance(effect, _MetaWatchNotification):
                        with self._lock:
                            watcher = self._meta_watchers.get(effect.watch_id)
                            callback = (
                                watcher.callback
                                if watcher is not None and not watcher.closed
                                else None
                            )
                        if callback is not None:
                            callback()
                except Exception as exc:
                    failures.append(type(exc).__name__)
            return _FacadeEffectResult(tuple(failures))
        finally:
            self._effect_context.in_effect_worker = False


    def _pump_network_effects(self) -> None:
        lane = self._network_effect_lane
        if lane is None:
            return
        while True:
            with self._lock:
                available = self._network_due_spaces - self._network_active_spaces
                if not available or self._effect_closed:
                    return
                space_id = min(available)
                operation_id = uuid.uuid4().hex
                self._network_due_spaces.discard(space_id)
                self._network_active_spaces.add(space_id)
                self._network_effect_spaces[operation_id] = space_id
                generation = self._effect_generation
            admission = lane.submit(
                EffectRequest(operation_id, generation, space_id)
            )
            if admission is AdmissionResult.ACCEPTED:
                continue
            with self._lock:
                self._network_effect_spaces.pop(operation_id, None)
                self._network_active_spaces.discard(space_id)
                self._network_due_spaces.add(space_id)
            return


    def request_broadcast_drain(self, space_id: str) -> None:
        with self._lock:
            if (
                self._effect_closed
                or self._network_effect_draining
                or space_id in self._detached_spaces
            ):
                return
            self._ensure_effect_lane()
            self._network_due_spaces.add(space_id)
        self._pump_network_effects()


    def drain_broadcasts(
        self, space_ids: tuple[str, ...], timeout: float = 5.0
    ) -> bool:
        targets = frozenset(space_ids)
        if not targets:
            return True
        with self._lock:
            if self._effect_closed:
                return False
            self._ensure_effect_lane_locked()
            self._network_due_spaces.update(targets)
            self._network_effect_draining = True
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            self._pump_network_effects()
            with self._lock:
                lane = self._network_effect_lane
                active = self._network_active_spaces & targets
                due = self._network_due_spaces & targets
            outstanding = lane.snapshot().outstanding if lane is not None else 0
            if not active and not due and outstanding == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)


    def _submit_post_commit_effects(
        self,
        operation_id: str,
        generation: int,
        effects: tuple[object, ...],
    ) -> bool:
        callbacks = tuple(effects)
        lane = self._effect_lane
        submitted_callbacks = lane is not None and bool(callbacks)
        if submitted_callbacks:
            with self._lock:
                self._effect_sequence += 1
                sequence = self._effect_sequence
            lane.submit_reserved(
                EffectRequest(
                    operation_id,
                    generation,
                    _FacadeEffectBatch(sequence, callbacks),
                )
            )
        elif lane is not None:
            lane.cancel_reservation(operation_id, generation)
            with self._lock:
                self._effect_waiters.pop(operation_id, None)
        return submitted_callbacks


    def _on_effect_completion(self, command: object) -> None:
        if not isinstance(command, EffectCompleted):
            raise TypeError("orgfs effect owner received an invalid completion")
        with self._lock:
            space_id = self._network_effect_spaces.get(command.operation_id)
            can_retry = (
                space_id is not None
                and not self._effect_closed
                and not self._network_effect_draining
                and space_id not in self._detached_spaces
            )
        retry = bool(
            can_retry
            and space_id is not None
            and self._space_has_unbroadcast(space_id)
        )
        with self._lock:
            if space_id is not None:
                self._network_effect_spaces.pop(command.operation_id, None)
                self._network_active_spaces.discard(space_id)
                if retry and space_id not in self._detached_spaces:
                    self._network_due_spaces.add(space_id)
            if command.generation == self._effect_generation:
                if command.error is not None:
                    self._effect_failures.append(command.error)
                elif isinstance(command.result, _FacadeEffectResult):
                    self._effect_failures.extend(command.result.failures)
                del self._effect_failures[:-32]
            waiter = self._effect_waiters.pop(command.operation_id, None)
        lane = self._network_effect_lane if space_id is not None else self._effect_lane
        if lane is not None:
            lane.acknowledge(command.operation_id, command.generation)
        if waiter is not None:
            waiter.set()
        if space_id is not None:
            self._pump_network_effects()


    def _wait_for_effect(self, operation_id: str | None) -> None:
        if operation_id is None:
            return
        with self._lock:
            waiter = self._effect_waiters.get(operation_id)
        if waiter is not None:
            waiter.wait(60.0)


    def detach_space(self, space_id: str, timeout: float = 5.0) -> bool:
        """Fence one space, drain its command loop and effects, and drop its facade state."""

        deadline = time.monotonic() + timeout
        with self._lock:
            self._detached_spaces.add(space_id)
            self._network_due_spaces.discard(space_id)
            owner = self._space_state_owners.get(space_id)
        if owner is not None:
            while time.monotonic() < deadline:
                snapshot = owner.runtime.snapshot()
                if (
                    snapshot.queued == 0
                    and snapshot.in_flight == 0
                    and not owner.deferred
                ):
                    break
                time.sleep(0.005)
            else:
                return False
            if not owner.effects.close(max(0.0, deadline - time.monotonic())):
                return False
            if not owner.runtime.stop(
                timeout=max(0.0, deadline - time.monotonic())
            ):
                return False
        while time.monotonic() < deadline:
            with self._lock:
                network_drained = space_id not in self._network_active_spaces
                effects_drained = not self._effect_waiters
            if network_drained and effects_drained:
                break
            time.sleep(0.005)
        else:
            return False
        with self._space_lock(space_id):
            with self._lock:
                space = self._spaces.pop(space_id, None)
                if space is not None:
                    for watcher in (*space.watches, *space.meta_watches):
                        watcher.close()
                        self._watchers.pop(watcher.watch_id, None)
                        self._meta_watchers.pop(watcher.watch_id, None)
                self._space_state_owners.pop(space_id, None)
                self._events.pop(space_id, None)
                self._space_locks.pop(space_id, None)
        return True


    def close_effects(self, timeout: float = 5.0) -> bool:
        with self._lock:
            self._space_state_closing = True
            state_owners = tuple(self._space_state_owners.items())
            lane = self._effect_lane
            network_lane = self._network_effect_lane
            cell = self._effect_cell
        deadline = time.monotonic() + timeout
        for _space_id, owner in state_owners:
            while time.monotonic() < deadline:
                snapshot = owner.runtime.snapshot()
                if (
                    snapshot.queued == 0
                    and snapshot.in_flight == 0
                    and not owner.deferred
                ):
                    break
                time.sleep(0.005)
            else:
                return False
            if not owner.effects.close(max(0.0, deadline - time.monotonic())):
                return False
            if not owner.runtime.stop(
                timeout=max(0.0, deadline - time.monotonic())
            ):
                return False
        with self._lock:
            self._effect_closed = True
        self._network_effect_stop.set()
        if lane is not None:
            if not lane.close(max(0.0, deadline - time.monotonic())):
                return False
            if network_lane is not None and not network_lane.close(
                max(0.0, deadline - time.monotonic())
            ):
                return False
            with self._lock:
                self._effect_generation += 1
            if cell is not None:
                if not cell.stop(timeout=max(0.0, deadline - time.monotonic())):
                    return False
            with self._lock:
                self._effect_lane = None
                self._network_effect_lane = None
                self._effect_cell = None
        with self._lock:
            self._space_state_owners.clear()
        return self._actor_hosts.shutdown(max(0.0, deadline - time.monotonic()))


    def __del__(self) -> None:
        try:
            self.close_effects(timeout=1.0)
        except Exception:
            pass


    @contextmanager
    def _post_commit_scope(self, space_id: str | None = None):
        """Reserve bounded observer custody around document writes."""

        outermost = not hasattr(self._effect_context, "pending")
        wait_for_callbacks = False
        operation_id: str | None = None
        generation = self._effect_generation
        reserved = False
        effects: tuple[object, ...] = ()
        authority_lock = self._lock if space_id is None else self._space_lock(space_id)
        try:
            with authority_lock:
                generation = self._effect_generation
                if outermost and self._needs_post_commit_effects():
                    self._ensure_effect_lane()
                    operation_id = uuid.uuid4().hex
                    admission = self._effect_lane.reserve(operation_id, generation)
                    if admission is not AdmissionResult.ACCEPTED:
                        raise OrgFsError(
                            "resource-exhausted",
                            {
                                "message": "orgfs post-commit effect lane is "
                                + admission.value
                            },
                        )
                    reserved = True
                    with self._lock:
                        self._effect_waiters[operation_id] = threading.Event()
                if outermost:
                    self._effect_context.pending = []
                try:
                    yield
                finally:
                    if outermost:
                        effects = tuple(self._effect_context.pending)
                        del self._effect_context.pending
        finally:
            if operation_id is not None and reserved:
                wait_for_callbacks = self._submit_post_commit_effects(
                    operation_id, generation, effects
                )
            if wait_for_callbacks and not getattr(
                self._effect_context, "in_effect_worker", False
            ):
                self._wait_for_effect(operation_id)


    def _space_lock(self, space_id: str) -> threading.RLock:
        with self._lock:
            lock = self._space_locks.get(space_id)
            if lock is None:
                lock = threading.RLock()
                self._space_locks[space_id] = lock
            return lock


