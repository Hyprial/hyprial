from __future__ import annotations





from threading import Lock

import threading

import time

from typing import Any, Mapping

import uuid

import weakref

from hyprial.kernel import (
    ActorEvent,
    ActorEventKind,
    ActorRuntime,
    ActorSpec,
    AdmissionResult)

from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest



from hyprial.daemon.impl.orgfs.api  import OrgFsError, SpaceInfo




from hyprial.daemon.impl.orgfs.mesh  import (
    OrgFsMesh)

from hyprial.daemon.impl.orgfs.storage.replica  import FsReplicaBackend, MemoryReplicaBackend, ReplicaStore

from hyprial.daemon.impl.orgfs.storage.replica_authority  import ReplicaAuthority

from hyprial.daemon.impl.orgfs.storage.space_authority  import _ReadStore

from hyprial.daemon.impl.orgfs.storage.store  import CommitRecord, LocalSpaceStore, StoreError


from hyprial.daemon.impl.orgfs.composition.vocabulary import _CheckoutSpace, _DirectoryCallOperation, _JoinSpace, _LifecycleAuthority, _LifecycleCommand, _LifecycleFailure, _LifecycleOutcome, _LifecycleWaiter, _MeshCreation, _OpenSpaceMesh, _ServeSpace, _SpaceResourceHandle

class RuntimeLifecycle:
    """Responsibility methods on the sole OrgFsRuntime state host.

    This class never constructs, copies, or persists an independent host.
    """

    def _ensure_lifecycle_authority(self) -> _LifecycleAuthority:
        with self._lifecycle_lock:
            if self._lifecycle_authority is not None:
                return self._lifecycle_authority
            owner_ref = weakref.ref(self)

            def actor_event(event: ActorEvent) -> None:
                owner = owner_ref()
                if owner is not None:
                    owner._on_lifecycle_event(event)

            def handle(command: object) -> None:
                owner = owner_ref()
                if owner is not None:
                    owner._on_lifecycle_command(command)

            def execute(command: _LifecycleCommand) -> _LifecycleOutcome:
                owner = owner_ref()
                if owner is None:
                    return _LifecycleOutcome(
                        error=_LifecycleFailure(
                            "OrgFsError", "unavailable", "orgfs runtime is closed", ()
                        )
                    )
                return owner._execute_lifecycle(command)

            runtime = ActorRuntime(event_sink=actor_event)
            actor = runtime.start(
                ActorSpec(
                    "orgfs-directory-lifecycle",
                    lambda: handle,
                    mailbox_capacity=64,
                    supervision_profile="state_authority",
                )
            )

            def complete(
                completion: EffectCompleted[_LifecycleOutcome],
            ) -> AdmissionResult:
                owner = owner_ref()
                if owner is None:
                    return AdmissionResult.CLOSED
                return runtime.tell(actor, completion)

            authority = _LifecycleAuthority(
                runtime,
                actor,
                EffectLane(
                    name="orgfs-directory-lifecycle-effects",
                    execute=execute,
                    complete=complete,
                    capacity=64,
                    workers=4,
                ),
            )
            self._lifecycle_authority = authority
            return authority


    def _execute_lifecycle(self, command: _LifecycleCommand) -> _LifecycleOutcome:
        self._lifecycle_context.running = True
        try:
            operation = command.operation
            if isinstance(operation, _JoinSpace):
                value = self._join_sync(operation.space_id)
            elif isinstance(operation, _ServeSpace):
                value = self._serve_sync(operation.space_id, operation.backend)
            elif isinstance(operation, _CheckoutSpace):
                value = self._checkout_sync(operation.space_id, operation.enabled)
            elif isinstance(operation, _OpenSpaceMesh):
                value = self._ensure_mesh_sync(operation.space_id)
            else:
                raise TypeError(f"unsupported directory lifecycle command {type(operation).__name__}")
            if isinstance(value, Mapping):
                value = tuple(sorted(value.items()))
            return _LifecycleOutcome(value=value)
        except Exception as error:
            details = getattr(error, "details", {})
            frozen = (
                tuple(sorted((str(key), str(value)) for key, value in dict(details).items()))
                if isinstance(details, dict)
                else ()
            )
            return _LifecycleOutcome(
                error=_LifecycleFailure(
                    type(error).__name__,
                    str(getattr(error, "code", "internal")),
                    (
                        error.message
                        if isinstance(error, OrgFsError)
                        else str(error)
                    ),
                    frozen,
                )
            )
        finally:
            self._lifecycle_context.running = False


    def _on_lifecycle_command(self, command: object) -> None:
        authority = self._lifecycle_authority
        if authority is None:
            return
        if isinstance(command, EffectCompleted):
            with self._lifecycle_lock:
                current = authority.commands.get(command.operation_id)
                if current is None or current.generation != command.generation:
                    waiter = None
                    aliases = ()
                else:
                    authority.commands.pop(command.operation_id, None)
                    authority.submitted.discard(command.operation_id)
                    waiter = authority.waiters.pop(command.operation_id, None)
                    aliases = tuple(authority.mesh_aliases.pop(command.operation_id, ()))
                    if isinstance(current.operation, _OpenSpaceMesh):
                        authority.mesh_opening.pop(current.operation.space_id, None)
            if waiter is not None:
                outcome = command.result
                if isinstance(outcome, _LifecycleOutcome):
                    waiter.value = outcome.value
                    waiter.error = outcome.error
                else:
                    waiter.error = _LifecycleFailure(
                        "RuntimeError", "internal", "invalid lifecycle result", ()
                    )
                waiter.done.set()
            if aliases:
                outcome = command.result
                for alias_id in aliases:
                    with self._lifecycle_lock:
                        alias_waiter = authority.waiters.pop(alias_id, None)
                        authority.commands.pop(alias_id, None)
                    if alias_waiter is None:
                        continue
                    if isinstance(outcome, _LifecycleOutcome):
                        alias_waiter.value = outcome.value
                        alias_waiter.error = outcome.error
                    else:
                        alias_waiter.error = _LifecycleFailure(
                            "RuntimeError", "internal", "invalid lifecycle result", ()
                        )
                    alias_waiter.done.set()
            authority.effects.acknowledge(command.operation_id, command.generation)
            self._pump_lifecycle(authority)
            return
        if not isinstance(command, _LifecycleCommand):
            raise TypeError("orgfs lifecycle actor received an invalid command")
        if isinstance(command.operation, _OpenSpaceMesh):
            with self._lifecycle_lock:
                opening = authority.mesh_opening.get(command.operation.space_id)
                if opening is not None and opening != command.operation_id:
                    aliases = authority.mesh_aliases.setdefault(opening, [])
                    if command.operation_id not in aliases:
                        aliases.append(command.operation_id)
                    return
                authority.mesh_opening[command.operation.space_id] = command.operation_id
        admission = authority.effects.submit(
            EffectRequest(command.operation_id, command.generation, command)
        )
        if admission is AdmissionResult.ACCEPTED:
            authority.deferred = [
                item for item in authority.deferred
                if item.operation_id != command.operation_id
            ]
            with self._lifecycle_lock:
                authority.submitted.add(command.operation_id)
            return
        if admission is AdmissionResult.OVERLOADED:
            if all(item.operation_id != command.operation_id for item in authority.deferred):
                authority.deferred.append(command)
            return


    def _pump_lifecycle(self, authority: _LifecycleAuthority) -> None:
        if not authority.deferred:
            return
        command = authority.deferred[0]
        admission = authority.effects.submit(
            EffectRequest(command.operation_id, command.generation, command)
        )
        if admission is AdmissionResult.ACCEPTED:
            authority.deferred.pop(0)
            with self._lifecycle_lock:
                authority.submitted.add(command.operation_id)
        elif admission is AdmissionResult.CLOSED:
            authority.deferred.pop(0)
            with self._lifecycle_lock:
                authority.commands.pop(command.operation_id, None)
                waiter = authority.waiters.pop(command.operation_id, None)
            if waiter is not None:
                waiter.error = _LifecycleFailure(
                    "OrgFsError", "unavailable", "directory lifecycle effects are closed", ()
                )
                waiter.done.set()


    def _on_lifecycle_event(self, event: ActorEvent) -> None:
        if event.kind is not ActorEventKind.CHILD_RESTARTED:
            return
        authority = self._lifecycle_authority
        if authority is None:
            return
        with self._lifecycle_lock:
            if event.generation <= authority.generation:
                return
            authority.generation = event.generation
            replay = tuple(
                _LifecycleCommand(
                    call.operation_id, event.generation, call.operation
                )
                for operation_id, call in authority.commands.items()
                if operation_id not in authority.submitted
            )
            for call in replay:
                authority.commands[call.operation_id] = call
        for call in replay:
            admission = authority.runtime.tell(authority.actor, call)
            if admission is AdmissionResult.CLOSED:
                return


    def _ask_lifecycle(self, operation: _DirectoryCallOperation) -> object:
        if getattr(self._lifecycle_context, "running", False):
            if isinstance(operation, _JoinSpace):
                return self._join_sync(operation.space_id)
            if isinstance(operation, _ServeSpace):
                return self._serve_sync(operation.space_id, operation.backend)
            if isinstance(operation, _CheckoutSpace):
                return self._checkout_sync(operation.space_id, operation.enabled)
            if isinstance(operation, _OpenSpaceMesh):
                return self._ensure_mesh_sync(operation.space_id)
        authority = self._ensure_lifecycle_authority()
        operation_id = uuid.uuid4().hex
        waiter = _LifecycleWaiter(threading.Event())
        with self._lifecycle_lock:
            if self._lifecycle_closing:
                raise OrgFsError("unavailable", {"message": "directory lifecycle is closing"})
            command = _LifecycleCommand(operation_id, authority.generation, operation)
            authority.commands[operation_id] = command
            authority.waiters[operation_id] = waiter
        while True:
            admission = authority.runtime.tell(authority.actor, command)
            if admission is AdmissionResult.ACCEPTED:
                break
            with self._lifecycle_lock:
                current = authority.commands.get(operation_id)
                if admission is AdmissionResult.CLOSED and current is not None and current.generation != command.generation:
                    command = current
                    continue
                authority.commands.pop(operation_id, None)
                authority.waiters.pop(operation_id, None)
            raise OrgFsError(
                "resource-exhausted" if admission is AdmissionResult.OVERLOADED else "unavailable",
                {"message": "directory lifecycle admission was " + admission.value},
            )
        waiter.done.wait()
        if waiter.error is not None:
            error = waiter.error
            details = {key: value for key, value in error.details}
            if error.kind == "OrgFsError":
                raise OrgFsError(error.code, {**details, "message": error.message})
            raise RuntimeError(error.message)
        if isinstance(waiter.value, tuple) and all(
            isinstance(item, tuple) and len(item) == 2 for item in waiter.value
        ):
            return dict(waiter.value)
        return waiter.value


    def _close_lifecycle(self, timeout: float = 5.0) -> bool:
        with self._lifecycle_lock:
            self._lifecycle_closing = True
            authority = self._lifecycle_authority
        if authority is None:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = authority.runtime.snapshot(authority.actor)
            if (
                snapshot.queued == 0
                and snapshot.in_flight == 0
                and not authority.deferred
            ):
                break
            time.sleep(0.005)
        else:
            return False
        if not authority.effects.close(max(0.0, deadline - time.monotonic())):
            return False
        if not authority.runtime.stop(
            authority.actor, timeout=max(0.0, deadline - time.monotonic())
        ):
            return False
        with self._lifecycle_lock:
            self._lifecycle_authority = None
        return True


    def _holder_snapshot(self, space_id: str) -> tuple[str, ...]:
        with self._pending_announces_lock:
            return tuple(sorted(self._holders.get(space_id, ())))


    def _known_holder_snapshot(self, space_id: str) -> tuple[str, ...]:
        with self._pending_announces_lock:
            return tuple(sorted(self._known_holders.get(space_id, ())))


    def _ensure_mesh_sync(self, space_id: str) -> _SpaceResourceHandle | None:
        with self._resource_lock:
            existing = self._meshes.get(space_id)
            if existing is not None:
                return _SpaceResourceHandle(space_id, self._directory_generation)
            creation = self._mesh_creations.get(space_id)
            if creation is None:
                creation = _MeshCreation()
                self._mesh_creations[space_id] = creation
                creator = True
            else:
                creator = False
        if not creator:
            creation.done.wait()
            if creation.error is not None:
                raise creation.error
            return creation.handle
        try:
            handle = self._create_mesh_sync(space_id)
        except BaseException as error:
            with self._resource_lock:
                creation.error = error
                self._mesh_creations.pop(space_id, None)
                creation.done.set()
            raise
        with self._resource_lock:
            creation.handle = handle
            self._mesh_creations.pop(space_id, None)
            creation.done.set()
        return handle


    def _create_mesh_sync(self, space_id: str) -> _SpaceResourceHandle | None:
        with self._resource_lock:
            session = self._session
            if self._closing or session is None:
                return None
            existing = self._meshes.get(space_id)
        if existing is not None:
            return _SpaceResourceHandle(space_id, self._directory_generation)

        def applied(envelope: bytes) -> tuple[str, ...]:
            self.facade.apply_envelope(space_id, envelope)
            self._reconcile_retirements(space_id)
            return self.facade.retained_blob_digests(space_id)

        def replacement(
            old_doc_id: str, new_doc_id: str, snapshot_bytes: bytes
        ) -> None:
            if space_id in self.facade._spaces:  # noqa: SLF001
                self.facade.install_replacement_snapshot(
                    space_id, old_doc_id, new_doc_id, snapshot_bytes
                )
                self._reconcile_retirements(space_id)

        mesh = OrgFsMesh(
            session,
            self.stores.get(space_id),
            self.node_id,
            author=self.author,
            blob_store=self.blobs,
            supplier_online=self._supplier_online,
            announcement_source=self._announcement_spaces,
            holder_discovery=lambda: self._holder_snapshot(space_id),
            recovery_candidates=lambda: (
                *self._holder_snapshot(space_id), *self._holder_candidates()
            ),
            on_applied=applied,
            on_replacement=replacement,
            logger=self.logger,
            space_authority=self.facade.space_authority(
                space_id, self.stores.get(space_id)
            ),
        )
        with self._resource_lock:
            if self._closing or self._session is not session:
                current = None
                discard = True
            else:
                current = self._meshes.get(space_id)
                if current is None:
                    current = mesh
                    self._meshes[space_id] = mesh
                discard = current is not mesh
            replica = self._replicas.get(space_id)
        if discard:
            mesh.close()
            return (
                _SpaceResourceHandle(space_id, self._directory_generation)
                if current is not None
                else None
            )
        if replica is not None:
            mesh.attach_replica(replica)
        self._announce_all()
        return _SpaceResourceHandle(space_id, self._directory_generation)


    def _mesh(self, space_id: str) -> OrgFsMesh | None:
        with self._resource_lock:
            if self._closing or self._session is None:
                return None
            existing = self._meshes.get(space_id)
        if existing is not None:
            return existing
        try:
            handle = self._ask_lifecycle(_OpenSpaceMesh(space_id))
        except OrgFsError as error:
            if error.code == "unavailable":
                return None
            raise
        if not isinstance(handle, _SpaceResourceHandle):
            return None
        with self._resource_lock:
            return self._meshes.get(handle.space_id)


    def _reconcile_retirements(self, space_id: str) -> None:
        """Finish G2 cleanup and ack after all replacement bytes are local."""

        store = self.stores.get(space_id)
        authority = self.facade.space_authority(space_id, store)
        records = tuple(authority.read(_ReadStore("retirement_records")))
        if authority.read(_ReadStore("pending_replacements")):
            return
        with self._resource_lock:
            replica = self._replicas.get(space_id)
        purge_entries = dict(authority.read(_ReadStore("purge_list_entries")))
        retirement_plan_ids = {record.plan_id for record in records}
        for record in records:
            snapshot = authority.read(
                _ReadStore("snapshot", doc_id=record.replacement_doc_id)
            )
            if replica is not None:
                plan_blobs = tuple(
                    {"sha": digest}
                    for digest, plan_id in sorted(purge_entries.items())
                    if plan_id == record.plan_id
                )
                replica.apply_retirement(
                    {
                        "oldPhysicalDocId": record.old_physical_doc_id,
                        "replacementDocId": record.replacement_doc_id,
                        "snapshotId": record.snapshot_id,
                        "replacementSnapshotBytes": snapshot.snapshot_bytes,
                    },
                    {
                        # R4: this replicated purge-list inventory is owner-authored
                        # and survives replica log retention.  Reconstructing the
                        # delete set from remaining rows loses trimmed updateBlob
                        # digests and can acknowledge with forbidden residue.
                        "blobs": plan_blobs,
                    },
                )
            authority.delete_retired_objects(record.old_physical_doc_id)
            if authority.read(
                _ReadStore("retired_residue", doc_id=record.old_physical_doc_id)
            ):
                raise StoreError("invalid-argument", "retired document residue remains")

        if replica is not None:
            replica.apply_blob_purge(
                digest
                for digest, plan_id in purge_entries.items()
                if plan_id not in retirement_plan_ids
            )
            for doc_id in authority.read(_ReadStore("document_ids")):
                if authority.read(_ReadStore("snapshot_point", doc_id=doc_id)) is not None:
                    replica.apply_retention(doc_id)
        for digest in purge_entries:
            self.blobs.release(space_id, digest)
            self.blobs.delete_if_unreferenced(digest)

        plans = {record.plan_id for record in records} | set(purge_entries.values())
        for plan_id in sorted(plans):
            if any(
                record.plan_id == plan_id
                and authority.read(
                    _ReadStore("retired_residue", doc_id=record.old_physical_doc_id)
                )
                for record in records
            ):
                continue
            self.facade.acknowledge_purge(space_id, plan_id)


    def broadcast_pending(self, space_id: str) -> int:
        mesh = self._mesh(space_id)
        return 0 if mesh is None else mesh.broadcast_pending()


    def broadcast_records(
        self, space_id: str, records: tuple[CommitRecord, ...]
    ) -> int:
        mesh = self._mesh(space_id)
        if mesh is None:
            return 0
        if "broadcast_pending" in vars(mesh):
            return mesh.broadcast_pending(space_id)
        return mesh.broadcast_records(records)


    def reconcile_replica_blobs(self, space_id: str) -> tuple[str, ...]:
        """Reconcile retained local blob refs through the per-space authority."""

        with self._resource_lock:
            replica = self._replicas.get(space_id)
        if replica is None:
            return ()
        reconciled = replica.reconcile_blobs()
        for digest in reconciled:
            self.blobs.pin(space_id, digest, "replica")
        return reconciled


    def join(self, space_id: str) -> SpaceInfo:
        return self._ask_lifecycle(_JoinSpace(space_id))  # type: ignore[return-value]


    def _join_sync(self, space_id: str) -> SpaceInfo:
        holders = sorted(
            {
                *(
                    node
                    for node in self._holder_snapshot(space_id)
                    if node != self.node_id
                ),
                *(node for node in self._holder_candidates() if node != self.node_id),
            }
        )
        if not holders:
            raise OrgFsError("no-holder-online", {"spaceId": space_id})
        mesh = self._mesh(space_id)
        assert mesh is not None
        last_error: Exception | None = None
        for holder in holders:
            try:
                mesh.sync_from(holder)
                info = self.facade.load_space(space_id)
                self._reconcile_retirements(space_id)
                self._announce_all()
                return info
            except (StoreError, OrgFsError) as exc:
                last_error = exc
        if last_error is not None:
            code = str(getattr(last_error, "code", "no-holder-online"))
            message = str(last_error).removeprefix(f"{code}: ")
            details = getattr(last_error, "details", {})
            raise OrgFsError(
                code,
                {
                    **(dict(details) if isinstance(details, dict) else {}),
                    "message": message,
                },
            ) from last_error
        raise OrgFsError("no-holder-online", {"spaceId": space_id})


    def watch_events(
        self, space_id: str, glob: str, since_version: str | None
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "spaceId": event.space_id,
                "kind": event.kind,
                "node": event.node,
                "oldPath": event.old_path,
            }
            for event in self.facade.changes_since(space_id, glob, since_version)
        )


    def serve(self, space_id: str, backend: str = "fs") -> dict[str, object]:
        return self._ask_lifecycle(_ServeSpace(space_id, backend))  # type: ignore[return-value]


    def _serve_sync(self, space_id: str, backend: str = "fs") -> dict[str, object]:
        if backend not in {"fs", "memory"}:
            raise OrgFsError(
                "invalid-argument", {"message": "backend must be fs or memory"}
            )
        try:
            self.facade.stat(space_id, "id:root")
        except OrgFsError as exc:
            if exc.code != "unknown-space":
                raise
            self.join(space_id)
        local_store = self.stores.get(space_id)
        with self._resource_lock:
            creation_lock = self._replica_creation_locks.setdefault(space_id, Lock())
        with creation_lock:
            return self._create_or_reuse_replica(space_id, backend, local_store)


    def _create_or_reuse_replica(
        self, space_id: str, backend: str, local_store: LocalSpaceStore
    ) -> dict[str, object]:
        """Own one space's replica construction before touching its backend."""

        with self._resource_lock:
            if self._closing:
                raise OrgFsError("unavailable", {"message": "orgfs runtime is closing"})
            existing = self._replicas.get(space_id)
        if existing is not None:
            existing_backend = "fs" if existing.backend_kind == "FsReplicaBackend" else "memory"
            if existing_backend != backend:
                raise OrgFsError(
                    "invalid-argument",
                    {
                        "message": "space is already served by another backend",
                        "spaceId": space_id,
                        "backend": existing_backend,
                    },
                )
            existing.reconcile_blobs()
            for digest in existing.pinned_blobs():
                self.blobs.pin(space_id, digest, "replica")
            self._reconcile_retirements(space_id)
            return {
                "spaceId": space_id,
                "backend": backend,
                "durable": existing.durable(),
                "pinnedBlobs": list(existing.pinned_blobs()),
            }
        replica_backend = (
            FsReplicaBackend(space_id, self.state_dir)
            if backend == "fs"
            else MemoryReplicaBackend(space_id)
        )

        def conflict_event(event: str, details: Any) -> None:
            if self.logger is not None:
                self.logger("error", f"orgfs.{event}", **dict(details))

        def owner_notice(event: str, details: Any) -> None:
            owner = next(
                info.owner for info in self.facade.spaces() if info.space_id == space_id
            )
            if self.logger is not None:
                self.logger(
                    "warn",
                    "orgfs.owner-notice",
                    notice=event,
                    owner=owner,
                    **dict(details),
                )
            if self.owner_notifier is not None:
                self.owner_notifier(owner, event, dict(details))

        replica_core = ReplicaStore(
            space_id, replica_backend, local_store,
            event_callback=conflict_event, owner_notifier=owner_notice,
        )
        replica = ReplicaAuthority(replica_core, local_store, self.blobs)
        replica.prime_from(local_store, self.blobs)
        for digest in replica.pinned_blobs():
            self.blobs.pin(space_id, digest, "replica")
        with self._resource_lock:
            if self._closing:
                replica.close(timeout=5.0)
                raise OrgFsError("unavailable", {"message": "orgfs runtime is closing"})
            self._replicas[space_id] = replica
        mesh = self._mesh(space_id)
        if mesh is not None:
            mesh.attach_replica(replica)
        self._reconcile_retirements(space_id)
        return {
            "spaceId": space_id,
            "backend": backend,
            "durable": replica.durable(),
            "pinnedBlobs": list(replica.pinned_blobs()),
        }
