from __future__ import annotations









import inspect


from pathlib import Path

import threading


import uuid

import weakref

from typing import Any, Callable, Mapping


from hyprial.kernel import (
    ActorEvent,
    ActorEventKind,
    ActorRuntime,
    ActorSpec,
    AdmissionResult)

from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest




from hyprial.daemon.impl.orgfs.api  import (
    OrgFsError)




from hyprial.daemon.impl.orgfs.storage.space_authority  import (
    _JsonProjection)



from hyprial.daemon.impl.orgfs.storage.store  import StoreError


from hyprial.daemon.impl.orgfs.document.model import _AcknowledgePurge, _ApplyContentUpdate, _ApplyEnvelope, _ApplyStructuredUpdate, _ApplyTreeUpdate, _CreateSpace, _FacadeEffectBatch, _HydrateContentSnapshot, _InstallReplacement, _InviteMember, _LoadSpace, _Mkdir, _Move, _Purge, _PurgePlanCommand, _RemoveMember, _RemoveNode, _Restore, _SPACE_STATE_CAPACITY, _SpaceStateCommand, _SpaceStateFailure, _SpaceStateOperation, _SpaceStateOutcome, _SpaceStateOwner, _SpaceStateWaiter, _Unban, _WriteBytes, _WriteText

class FacadeSpaceState:
    """Responsibility methods on the sole LocalOrgFs state host.

    This class never constructs, copies, or persists an independent host.
    """

    def _make_space_operation(
        self, method: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> _SpaceStateOperation:
        bound = inspect.signature(method).bind(self, *args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        name = method.__name__
        if name == "create_space":
            return _CreateSpace(values["name"])
        if name == "load_space":
            return _LoadSpace(values["space_id"])
        if name == "invite":
            return _InviteMember(values["space_id"], values["user"], values["mode"])
        if name == "remove_member":
            return _RemoveMember(values["space_id"], values["user"])
        if name == "apply_envelope":
            return _ApplyEnvelope(values["space_id"], bytes(values["envelope"]))
        if name == "install_replacement_snapshot":
            return _InstallReplacement(
                values["space_id"], values["old_doc_id"], values["new_doc_id"],
                bytes(values["snapshot_bytes"]),
            )
        if name == "acknowledge_purge":
            return _AcknowledgePurge(values["space_id"], values["plan_id"])
        if name == "write_text":
            return _WriteText(
                values["space_id"], values["node"], values["content"],
                values["base_version"], values["expect_version"],
            )
        if name == "write_bytes":
            return _WriteBytes(
                values["space_id"], values["node"], bytes(values["content"]),
                values["expect_version"],
            )
        if name == "mkdir":
            return _Mkdir(values["space_id"], values["path"])
        if name == "move":
            return _Move(values["space_id"], values["source"], values["destination"])
        if name == "remove":
            return _RemoveNode(values["space_id"], values["node"])
        if name == "restore":
            return _Restore(
                values["space_id"], values["node"], values["version"],
                bool(values["recursive"]),
            )
        if name == "purge_plan":
            targets = tuple(
                tuple(sorted(self._freeze_space_value(dict(item))))
                for item in values["targets"]
            )
            return _PurgePlanCommand(values["space_id"], targets)
        if name == "purge":
            return _Purge(values["space_id"], values["plan_id"])
        if name == "unban":
            return _Unban(values["space_id"], values["sha"])
        if name == "apply_tree_update":
            return _ApplyTreeUpdate(values["space_id"], bytes(values["update"]))
        if name == "apply_content_update":
            return _ApplyContentUpdate(
                values["space_id"], values["node"], bytes(values["update"])
            )
        if name == "hydrate_content_snapshot":
            return _HydrateContentSnapshot(
                values["space_id"], values["node"], values["expected_doc_id"],
                bytes(values["snapshot"])
            )
        if name == "_apply_structured_update":
            return _ApplyStructuredUpdate(
                values["space_id"], values["node_id"], values["doc_id"],
                int(values["client_id"]), bytes(values["base_state"]),
                bytes(values["update"]),
            )
        raise ValueError(f"no frozen OrgSpace command for {name}")


    @staticmethod
    def _freeze_space_value(value: object) -> object:
        if isinstance(value, Mapping):
            return tuple(
                sorted(
                    (str(key), FacadeSpaceState._freeze_space_value(item))
                    for key, item in value.items()
                )
            )
        if isinstance(value, (tuple, list)):
            return tuple(FacadeSpaceState._freeze_space_value(item) for item in value)
        if value is None or isinstance(value, (str, bytes, int, float, bool, Path)):
            return value
        return str(value)


    @staticmethod
    def _thaw_space_value(value: object) -> object:
        if isinstance(value, _JsonProjection):
            return value.decode()
        if isinstance(value, tuple) and all(
            isinstance(item, tuple)
            and len(item) == 2
            and isinstance(item[0], str)
            for item in value
        ):
            return {key: FacadeSpaceState._thaw_space_value(item) for key, item in value}
        if isinstance(value, tuple):
            return [FacadeSpaceState._thaw_space_value(item) for item in value]
        return value


    def _ensure_space_state_owner(self, space_id: str) -> _SpaceStateOwner:
        with self._lock:
            existing = self._space_state_owners.get(space_id)
            if existing is not None:
                return existing
            owner_ref = weakref.ref(self)

            def actor_event(event: ActorEvent) -> None:
                owner = owner_ref()
                if owner is not None:
                    owner._on_space_state_event(space_id, event)

            def handle(command: object) -> None:
                owner = owner_ref()
                if owner is not None:
                    owner._on_space_state_command(space_id, command)

            def execute(command: _SpaceStateCommand) -> _SpaceStateOutcome:
                owner = owner_ref()
                if owner is None:
                    return _SpaceStateOutcome(
                        error=_SpaceStateFailure(
                            "OrgFsError", "unavailable", "orgfs is closed", ()
                        )
                    )
                return owner._execute_space_state(space_id, command)

            runtime = ActorRuntime(event_sink=actor_event)
            actor = runtime.start(
                ActorSpec(
                    f"orgfs-space-{space_id[:12]}-state",
                    lambda: handle,
                    mailbox_capacity=_SPACE_STATE_CAPACITY,
                    supervision_profile="state_authority",
                )
            )

            def complete(
                completion: EffectCompleted[_SpaceStateOutcome],
            ) -> AdmissionResult:
                owner = owner_ref()
                if owner is None:
                    return AdmissionResult.CLOSED
                return runtime.tell(actor, completion)

            effects = EffectLane(
                name=f"orgfs-space-{space_id[:12]}-state-work",
                execute=execute,
                complete=complete,
                capacity=_SPACE_STATE_CAPACITY,
                workers=1,
            )
            created = _SpaceStateOwner(runtime, actor, effects)
            self._space_state_owners[space_id] = created
            return created


    def _execute_space_operation(self, operation: _SpaceStateOperation) -> object:
        if isinstance(operation, _CreateSpace):
            return type(self).create_space.__wrapped__(self, operation.name)
        if isinstance(operation, _LoadSpace):
            return type(self).load_space.__wrapped__(self, operation.space_id)
        if isinstance(operation, _InviteMember):
            return type(self).invite.__wrapped__(self, operation.space_id, operation.user, operation.mode)
        if isinstance(operation, _RemoveMember):
            return type(self).remove_member.__wrapped__(self, operation.space_id, operation.user)
        if isinstance(operation, _ApplyEnvelope):
            return type(self).apply_envelope.__wrapped__(self, operation.space_id, operation.envelope)
        if isinstance(operation, _InstallReplacement):
            return type(self).install_replacement_snapshot.__wrapped__(
                self, operation.space_id, operation.old_doc_id, operation.new_doc_id, operation.snapshot
            )
        if isinstance(operation, _AcknowledgePurge):
            return type(self).acknowledge_purge.__wrapped__(self, operation.space_id, operation.plan_id)
        if isinstance(operation, _WriteText):
            return type(self).write_text.__wrapped__(
                self, operation.space_id, operation.node, operation.content,
                base_version=operation.base_version, expect_version=operation.expect_version,
            )
        if isinstance(operation, _WriteBytes):
            return type(self).write_bytes.__wrapped__(
                self, operation.space_id, operation.node, operation.content,
                expect_version=operation.expect_version,
            )
        if isinstance(operation, _Mkdir):
            return type(self).mkdir.__wrapped__(self, operation.space_id, operation.path)
        if isinstance(operation, _Move):
            return type(self).move.__wrapped__(
                self, operation.space_id, operation.source, operation.destination
            )
        if isinstance(operation, _RemoveNode):
            return type(self).remove.__wrapped__(self, operation.space_id, operation.node)
        if isinstance(operation, _Restore):
            return type(self).restore.__wrapped__(
                self, operation.space_id, operation.node, operation.version,
                recursive=operation.recursive,
            )
        if isinstance(operation, _PurgePlanCommand):
            targets = tuple(self._thaw_space_value(target) for target in operation.targets)
            return type(self).purge_plan.__wrapped__(self, operation.space_id, targets)
        if isinstance(operation, _Purge):
            return type(self).purge.__wrapped__(self, operation.space_id, operation.plan_id)
        if isinstance(operation, _Unban):
            return type(self).unban.__wrapped__(self, operation.space_id, operation.sha)
        if isinstance(operation, _ApplyTreeUpdate):
            return type(self).apply_tree_update.__wrapped__(self, operation.space_id, operation.update)
        if isinstance(operation, _ApplyContentUpdate):
            return type(self).apply_content_update.__wrapped__(
                self, operation.space_id, operation.node, operation.update
            )
        if isinstance(operation, _HydrateContentSnapshot):
            return type(self).hydrate_content_snapshot.__wrapped__(
                self, operation.space_id, operation.node, operation.snapshot,
                expected_doc_id=operation.expected_doc_id,
            )
        if isinstance(operation, _ApplyStructuredUpdate):
            return type(self)._apply_structured_update.__wrapped__(
                self, operation.space_id, operation.node_id, operation.doc_id,
                operation.client_id, operation.base_state, operation.update,
            )
        raise TypeError(f"unsupported OrgSpace operation: {type(operation).__name__}")


    def _execute_space_state(
        self, space_id: str, command: _SpaceStateCommand
    ) -> _SpaceStateOutcome:
        self._space_state_context.running = True
        if not hasattr(self._effect_context, "pending"):
            self._effect_context.pending = []
        try:
            authority_lock = self._lock if space_id == "__directory__" else self._space_lock(space_id)
            with authority_lock:
                try:
                    value = self._execute_space_operation(command.operation)
                except Exception as error:
                    details = getattr(error, "details", {})
                    return _SpaceStateOutcome(
                        error=_SpaceStateFailure(
                            type(error).__name__,
                            str(getattr(error, "code", "internal")),
                            str(error),
                            tuple(
                                sorted(
                                    (str(key), self._freeze_space_value(item))
                                    for key, item in dict(details).items()
                                )
                            )
                            if isinstance(details, Mapping)
                            else (),
                        )
                    )
                finally:
                    effects = tuple(self._effect_context.pending)
                    if command.effect_operation_id is not None:
                        if effects:
                            with self._lock:
                                self._effect_sequence += 1
                                sequence = self._effect_sequence
                            self._effect_lane.submit_reserved(
                                EffectRequest(
                                    command.effect_operation_id,
                                    command.effect_generation,
                                    _FacadeEffectBatch(sequence, effects),
                                )
                            )
                        else:
                            self._effect_lane.cancel_reservation(
                                command.effect_operation_id, command.effect_generation
                            )
                            with self._lock:
                                self._effect_waiters.pop(command.effect_operation_id, None)
                    del self._effect_context.pending
                return _SpaceStateOutcome(value=value)
        finally:
            self._space_state_context.running = False


    def _on_space_state_command(self, space_id: str, command: object) -> None:
        owner = self._space_state_owners[space_id]
        if isinstance(command, EffectCompleted):
            with self._lock:
                state_command = owner.commands.get(command.operation_id)
                if state_command is None or state_command.generation != command.generation:
                    waiter = None
                else:
                    owner.commands.pop(command.operation_id, None)
                    owner.submitted.discard(command.operation_id)
                    waiter = owner.waiters.pop(command.operation_id, None)
            if waiter is not None:
                outcome = command.result
                if isinstance(outcome, _SpaceStateOutcome):
                    waiter.value = outcome.value
                    waiter.error = outcome.error
                else:
                    waiter.error = _SpaceStateFailure(
                        "RuntimeError", "internal", "invalid orgspace completion", ()
                    )
                waiter.done.set()
            owner.effects.acknowledge(command.operation_id, command.generation)
            self._pump_space_state(owner)
            return
        if not isinstance(command, _SpaceStateCommand):
            raise TypeError("orgspace actor received an invalid command")
        with self._lock:
            if command.generation != owner.generation:
                return
            admission = owner.effects.submit(
                EffectRequest(command.operation_id, command.generation, command)
            )
            if admission is AdmissionResult.ACCEPTED:
                owner.deferred = [
                    item
                    for item in owner.deferred
                    if item.operation_id != command.operation_id
                ]
                owner.submitted.add(command.operation_id)
                return
            if admission is AdmissionResult.OVERLOADED:
                if all(
                    item.operation_id != command.operation_id
                    for item in owner.deferred
                ):
                    owner.deferred.append(command)
                return
        with self._lock:
            waiter = owner.waiters.pop(command.operation_id, None)
            owner.commands.pop(command.operation_id, None)
        if waiter is not None:
            waiter.error = _SpaceStateFailure(
                "OrgFsError", "unavailable", "orgspace is closing", ()
            )
            waiter.done.set()


    def _pump_space_state(self, owner: _SpaceStateOwner) -> None:
        with self._lock:
            if not owner.deferred:
                return
            command = owner.deferred[0]
            admission = owner.effects.submit(
                EffectRequest(command.operation_id, command.generation, command)
            )
            if admission is AdmissionResult.ACCEPTED:
                owner.deferred.pop(0)
                owner.submitted.add(command.operation_id)


    def _on_space_state_event(self, space_id: str, event: ActorEvent) -> None:
        if event.kind is not ActorEventKind.CHILD_RESTARTED:
            return
        with self._lock:
            owner = self._space_state_owners.get(space_id)
            if owner is None or event.generation <= owner.generation:
                return
            owner.generation = event.generation
            deferred_ids = {command.operation_id for command in owner.deferred}
            replay = tuple(
                _SpaceStateCommand(
                    command.operation_id, event.generation, command.operation,
                    command.effect_operation_id, command.effect_generation,
                )
                for operation_id, command in owner.commands.items()
                if operation_id not in owner.submitted
            )
            for command in replay:
                owner.commands[command.operation_id] = command
            replacements = {command.operation_id: command for command in replay}
            owner.deferred = [
                replacements[command.operation_id]
                for command in owner.deferred
                if command.operation_id in replacements
            ]
        self._pump_space_state(owner)
        for command in replay:
            if command.operation_id in deferred_ids:
                continue
            admission = owner.runtime.tell(owner.actor, command)
            if admission is not AdmissionResult.ACCEPTED:
                with self._lock:
                    if all(
                        item.operation_id != command.operation_id
                        for item in owner.deferred
                    ):
                        owner.deferred.append(command)
                self._pump_space_state(owner)


    def _submit_space_state(
        self, method: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        operation = self._make_space_operation(method, args, kwargs)
        space_id = (
            "__directory__" if isinstance(operation, _CreateSpace) else operation.space_id
        )
        owner = self._ensure_space_state_owner(space_id)
        operation_id = uuid.uuid4().hex
        effect_id: str | None = None
        effect_generation = self._effect_generation
        waiter = _SpaceStateWaiter(threading.Event())
        with self._lock:
            if self._space_state_closing:
                raise OrgFsError("unavailable", {"message": "orgfs is closing"})
            if len(owner.commands) >= _SPACE_STATE_CAPACITY:
                raise OrgFsError(
                    "resource-exhausted",
                    {"message": "orgspace total custody is full"},
                )
            if self._needs_post_commit_effects():
                self._ensure_effect_lane()
                effect_id = uuid.uuid4().hex
                admission = self._effect_lane.reserve(effect_id, effect_generation)
                if admission is not AdmissionResult.ACCEPTED:
                    raise OrgFsError(
                        "resource-exhausted",
                        {
                            "message": "orgfs post-commit effect lane is "
                            + admission.value
                        },
                    )
                self._effect_waiters[effect_id] = threading.Event()
            command = _SpaceStateCommand(
                operation_id, owner.generation, operation, effect_id, effect_generation
            )
            owner.commands[operation_id] = command
            owner.waiters[operation_id] = waiter
        while True:
            admission = owner.runtime.tell(owner.actor, command)
            if admission is AdmissionResult.ACCEPTED:
                break
            with self._lock:
                current = owner.commands.get(operation_id)
                if (
                    admission is AdmissionResult.CLOSED
                    and current is not None
                    and current.generation != command.generation
                ):
                    command = current
                    continue
                owner.commands.pop(operation_id, None)
                owner.waiters.pop(operation_id, None)
                if effect_id is not None:
                    self._effect_lane.cancel_reservation(effect_id, effect_generation)
                    self._effect_waiters.pop(effect_id, None)
            raise OrgFsError(
                "resource-exhausted" if admission is AdmissionResult.OVERLOADED else "unavailable",
                {"message": "orgspace actor admission was " + admission.value},
            )
        waiter.done.wait()
        if effect_id is not None and not getattr(self._effect_context, "in_effect_worker", False):
            self._wait_for_effect(effect_id)
        if waiter.error is not None:
            error = waiter.error
            details = dict(error.details)
            if error.kind == "OrgFsError":
                raise OrgFsError(error.code, {**details, "message": error.message})
            if error.kind == "StoreError":
                raise StoreError(error.code, error.message, **details)
            if error.kind == "ValueError":
                raise ValueError(error.message)
            if error.kind == "TypeError":
                raise TypeError(error.message)
            if error.kind in {"FileNotFoundError", "PermissionError", "OSError"}:
                error_type = {
                    "FileNotFoundError": FileNotFoundError,
                    "PermissionError": PermissionError,
                    "OSError": OSError,
                }[error.kind]
                raise error_type(error.message)
            raise RuntimeError(error.message)
        return waiter.value
