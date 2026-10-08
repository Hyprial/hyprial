from __future__ import annotations



from contextlib import contextmanager







import json


import threading

import time

import uuid

import weakref

from typing import Any, Mapping


from hyprial.kernel import (
    ActorRuntime,
    ActorSpec,
    AdmissionResult)

from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest




from hyprial.daemon.impl.orgfs.api  import (
    OrgFsError)




from hyprial.daemon.impl.orgfs.storage.space_authority  import (
    OrgSpaceAuthority)



from hyprial.daemon.impl.orgfs.storage.store  import CommitRecord


from hyprial.daemon.impl.orgfs.document.model import _BroadcastPending, _FACADE_EFFECT_CAPACITY, _FacadeEffectBatch, _FacadeEffectResult, _ReconcileReplicaBlobs, _WatchNotification

class FacadeEffects:
    """Responsibility methods on the sole LocalOrgFs state host.

    This class never constructs, copies, or persists an independent host.
    """

    def _defer_effect(
        self, effect: _WatchNotification | _ReconcileReplicaBlobs | _BroadcastPending
    ) -> None:
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
        return has_watcher or (self.mesh is not None and self.stores is not None)


    def _broadcast_is_enabled(self) -> bool:
        if self.mesh is None or not hasattr(self.mesh, "broadcast_pending"):
            return False
        method = getattr(self.mesh, "broadcast_pending")
        implementation = getattr(type(self.mesh), "broadcast_pending", None)
        # Capture explicit instance overrides before the asynchronous effect
        # runs, preserving suppression/admission seams without caller closures.
        return implementation is None or getattr(method, "__func__", None) is implementation


    def _publication_records(
        self,
        space_id: str,
        current: tuple[CommitRecord, ...],
        *,
        broadcast: bool,
    ) -> tuple[CommitRecord, ...]:
        if not current:
            return ()
        enabled = broadcast and self._broadcast_is_enabled()
        # Publish durable outbox rows through the current journal transaction's
        # rowid frontier. This includes earlier writer registrations on another
        # lane that the current CRDT delta causally depends on, while excluding
        # later commits that happen to be visible in the database already.
        if enabled:
            store = self._store(space_id)
            keys: list[tuple[str, int, str]] = []
            for record in current:
                try:
                    doc_id = str(json.loads(record.envelope_bytes)["docId"])
                except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                keys.append((record.writer, record.seq, doc_id))
            authority = self._space_authorities.get(space_id)
            if keys and authority is not None and hasattr(authority, "unbroadcast_through"):
                current = (*current, *authority.unbroadcast_through(tuple(keys)))
            elif keys and store is not None and hasattr(store, "unbroadcast_through"):
                current = (*current, *store.unbroadcast_through(tuple(keys)))
        with self._lock:
            current = tuple(
                record
                for record in current
                if (record.writer, record.seq, record.envelope_bytes)
                not in self._broadcast_inflight
            )
            if not current:
                return ()
            pending = self._pending_outbox.setdefault(space_id, [])
            if not enabled:
                pending.extend(current)
                return ()
            combined = (*pending, *current)
            pending.clear()
        unique: dict[tuple[str, int, bytes], CommitRecord] = {}
        for record in combined:
            unique[(record.writer, record.seq, record.envelope_bytes)] = record
        result = tuple(unique.values())
        with self._lock:
            self._broadcast_inflight.update(
                (record.writer, record.seq, record.envelope_bytes)
                for record in result
            )
        return result


    def _ensure_effect_lane(self) -> None:
        if self._effect_closed:
            raise OrgFsError("unavailable", {"message": "orgfs is closing"})
        if self._effect_lane is not None:
            return
        runtime = ActorRuntime()
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

        handle = runtime.start(
            ActorSpec(
                "orgfs-post-commit-effects",
                lambda: handle_completion,
                mailbox_capacity=_FACADE_EFFECT_CAPACITY,
                supervision_profile="state_authority",
            )
        )
        self._effect_runtime = runtime
        self._effect_actor = handle
        self._effect_lane = EffectLane(
            name="orgfs-post-commit",
            execute=execute_batch,
            complete=lambda completion: runtime.tell(handle, completion),
            capacity=_FACADE_EFFECT_CAPACITY,
            workers=1,
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
                    elif isinstance(effect, _ReconcileReplicaBlobs):
                        mesh = self.mesh
                        if mesh is not None and hasattr(
                            mesh, "reconcile_replica_blobs"
                        ):
                            mesh.reconcile_replica_blobs(effect.space_id)
                    else:
                        mesh = self.mesh
                        if mesh is not None:
                            try:
                                if effect.records and hasattr(mesh, "broadcast_records"):
                                    mesh.broadcast_records(effect.space_id, effect.records)
                                elif hasattr(mesh, "broadcast_pending"):
                                    mesh.broadcast_pending(effect.space_id)
                            finally:
                                with self._lock:
                                    self._broadcast_inflight.difference_update(
                                        (record.writer, record.seq, record.envelope_bytes)
                                        for record in effect.records
                                    )
                except Exception as exc:
                    failures.append(type(exc).__name__)
            return _FacadeEffectResult(tuple(failures))
        finally:
            self._effect_context.in_effect_worker = False


    def _on_effect_completion(self, command: object) -> None:
        if not isinstance(command, EffectCompleted):
            raise TypeError("orgfs effect owner received an invalid completion")
        with self._lock:
            if command.generation == self._effect_generation:
                if command.error is not None:
                    self._effect_failures.append(command.error)
                elif isinstance(command.result, _FacadeEffectResult):
                    self._effect_failures.extend(command.result.failures)
                del self._effect_failures[:-32]
            waiter = self._effect_waiters.pop(command.operation_id, None)
        lane = self._effect_lane
        if lane is not None:
            lane.acknowledge(command.operation_id, command.generation)
        if waiter is not None:
            waiter.set()


    def _wait_for_effect(self, operation_id: str | None) -> None:
        if operation_id is None:
            return
        with self._lock:
            waiter = self._effect_waiters.get(operation_id)
        if waiter is not None:
            waiter.wait(60.0)


    def close_effects(self, timeout: float = 5.0) -> bool:
        with self._lock:
            self._space_state_closing = True
            state_owners = tuple(self._space_state_owners.items())
            lane = self._effect_lane
            runtime = self._effect_runtime
            actor = self._effect_actor
            authorities = tuple(self._space_authorities.values())
        deadline = time.monotonic() + timeout
        for _space_id, owner in state_owners:
            while time.monotonic() < deadline:
                snapshot = owner.runtime.snapshot(owner.actor)
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
                owner.actor, timeout=max(0.0, deadline - time.monotonic())
            ):
                return False
        with self._lock:
            self._effect_closed = True
        if lane is not None:
            if not lane.close(max(0.0, deadline - time.monotonic())):
                return False
            with self._lock:
                self._effect_generation += 1
            if runtime is not None and actor is not None:
                if not runtime.stop(actor, timeout=max(0.0, deadline - time.monotonic())):
                    return False
            with self._lock:
                self._effect_lane = None
                self._effect_runtime = None
                self._effect_actor = None
        for authority in authorities:
            if not authority.close(timeout=max(0.0, deadline - time.monotonic())):
                return False
        with self._lock:
            self._space_authorities.clear()
            self._space_state_owners.clear()
        return True


    def __del__(self) -> None:
        try:
            self.close_effects(timeout=1.0)
        except Exception:
            pass


    @contextmanager
    def _post_commit_scope(self, space_id: str | None = None):
        """Reserve bounded observer custody around structured-document writes."""

        outermost = not hasattr(self._effect_context, "pending")
        wait_for_completion = False
        operation_id: str | None = None
        authority_lock = self._lock if space_id is None else self._space_lock(space_id)
        with authority_lock:
            generation = self._effect_generation
            reserved = False
            if outermost and self._needs_post_commit_effects():
                self._ensure_effect_lane()
                operation_id = uuid.uuid4().hex
                admission = self._effect_lane.reserve(operation_id, generation)
                if admission is not AdmissionResult.ACCEPTED:
                    raise OrgFsError(
                        "resource-exhausted",
                        {"message": "orgfs post-commit effect lane is " + admission.value},
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
                    if operation_id is not None and reserved:
                        if effects:
                            with self._lock:
                                self._effect_sequence += 1
                                sequence = self._effect_sequence
                            self._effect_lane.submit_reserved(
                                EffectRequest(
                                    operation_id,
                                    generation,
                                    _FacadeEffectBatch(sequence, effects),
                                )
                            )
                            wait_for_completion = True
                        else:
                            self._effect_lane.cancel_reservation(
                                operation_id, generation
                            )
                            with self._lock:
                                self._effect_waiters.pop(operation_id, None)
        if (
            wait_for_completion
            and not getattr(self._effect_context, "in_effect_worker", False)
        ):
            self._wait_for_effect(operation_id)


    def _store(self, space_id: str) -> Any:
        if self.stores is None:
            return None
        if hasattr(self.stores, "commit"):
            return self.stores
        if isinstance(self.stores, Mapping):
            return self.stores.get(space_id)
        return None


    def _space_lock(self, space_id: str) -> threading.RLock:
        with self._lock:
            lock = self._space_locks.get(space_id)
            if lock is None:
                lock = threading.RLock()
                self._space_locks[space_id] = lock
            return lock


    def space_authority(self, space_id: str, store: Any | None = None) -> OrgSpaceAuthority | None:
        if self._effect_closed and not getattr(
            self._space_state_context, "running", False
        ):
            raise OrgFsError("unavailable", {"message": "orgfs is closing"})
        store = store if store is not None else self._store(space_id)
        if store is None or not hasattr(store, "commit_with_outbox"):
            return None
        with self._lock:
            authority = self._space_authorities.get(space_id)
            if authority is None:
                authority = OrgSpaceAuthority(space_id, store)
                self._space_authorities[space_id] = authority
            return authority
