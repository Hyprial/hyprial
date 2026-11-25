# Decision record (phase D, package S, 2026-11-25):
# - The journal mesh, replica attach, retirement reconciliation and the
#   outbox broadcast entry points are deleted with the journal store.  This
#   mixin keeps the per-space lifecycle actor (open, join, checkout commands)
#   that the state runtime drives; ``composition/state/sync.py`` supplies the
#   per-space ``SpaceSync`` creation and the join.
from __future__ import annotations


import threading

import time

from typing import Any, Mapping

import uuid

import weakref

from hyprial.kernel import (
    ActorEvent,
    ActorEventKind,
    ActorSpec,
    AdmissionResult)

from hyprial.kernel import EffectCompleted, EffectRequest


from hyprial.daemon.impl.orgfs.api  import OrgFsError, SpaceInfo


from hyprial.daemon.impl.orgfs.composition.vocabulary import _CheckoutSpace, _DirectoryCallOperation, _JoinSpace, _LifecycleAuthority, _LifecycleCommand, _LifecycleFailure, _LifecycleOutcome, _LifecycleWaiter, _MeshCreation, _OpenSpaceMesh, _SpaceResourceHandle

ORGFS_LIFECYCLE_WAIT_SECONDS = 120.0


class RuntimeLifecycle:
    """Responsibility methods on the sole OrgFsRuntime state host.

    This class never constructs, copies, or persists an independent host.
    """

    def _ensure_lifecycle_authority(self) -> _LifecycleAuthority:
        with self._lifecycle_lock:
            if self._lifecycle_closing:
                raise OrgFsError(
                    "unavailable", {"message": "directory lifecycle is closing"}
                )
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

            host = self._actor_hosts.host
            cell = host.spawn(
                ActorSpec(
                    "orgfs-directory-lifecycle",
                    lambda: handle,
                    mailbox_capacity=64,
                    supervision_profile="state_authority",
                    event_sink=actor_event,
                )
            )

            def complete(
                completion: EffectCompleted[_LifecycleOutcome],
            ) -> AdmissionResult:
                owner = owner_ref()
                if owner is None:
                    return AdmissionResult.CLOSED
                return cell.tell(completion)

            authority = _LifecycleAuthority(
                cell,
                host.effect_lane(
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
            admission = authority.runtime.tell(call)
            if admission is AdmissionResult.CLOSED:
                return


    def _ask_lifecycle(self, operation: _DirectoryCallOperation, *, timeout: float | None = None) -> object:
        with self._resource_lock:
            if operation.space_id in self._purging_spaces:
                raise OrgFsError("unknown-space", {"spaceId": operation.space_id})
        if getattr(self._lifecycle_context, "running", False):
            if isinstance(operation, _JoinSpace):
                return self._join_sync(operation.space_id)
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
            admission = authority.runtime.tell(command)
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
        wait = ORGFS_LIFECYCLE_WAIT_SECONDS if timeout is None else timeout
        started = time.monotonic()
        if not waiter.done.wait(wait):
            with self._lifecycle_lock:
                authority.waiters.pop(operation_id, None)
            if self.logger is not None:
                self.logger(
                    "warn", "orgfs.lifecycle.wait-timeout",
                    operation={_JoinSpace: "join", _CheckoutSpace: "checkout",
                               _OpenSpaceMesh: "open-mesh"}[type(operation)],
                    waitedSeconds=max(0.0, time.monotonic() - started),
                    budgetSeconds=wait,
                    commandId=operation_id,
                )
            raise OrgFsError("unavailable", {"message": "OrgFS lifecycle wait timed out",
                                           "leg": "join-orgfs" if isinstance(operation, _JoinSpace) else "orgfs-lifecycle",
                                           "budgetSeconds": wait})
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
            snapshot = authority.runtime.snapshot()
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
            timeout=max(0.0, deadline - time.monotonic())
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


    def _mesh(self, space_id: str) -> Any:
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


    def join(self, space_id: str, *, timeout: float | None = None) -> SpaceInfo:
        return self._ask_lifecycle(_JoinSpace(space_id), timeout=timeout)  # type: ignore[return-value]


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
