"""Bounded per-space authority for replica state and backend effects."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, is_dataclass
import threading
import time
import uuid
import weakref
from typing import Any

from hyprial.actor_runtime import (
    ActorEvent,
    ActorEventKind,
    ActorHandle,
    ActorRuntime,
    ActorSpec,
    AdmissionResult,
)
from hyprial.actor_runtime.effects import EffectCompleted, EffectLane, EffectRequest

from .api import OrgFsError
from .replica import PutOutcome, ReplicaStore, _digest
from .store import LocalSpaceStore

_REPLICA_CAPACITY = 128


@dataclass(frozen=True, slots=True)
class _PrimeReplica:
    pass


@dataclass(frozen=True, slots=True)
class _ReconcileBlobs:
    pass


@dataclass(frozen=True, slots=True)
class _StoreObject:
    kind: str
    key: str
    data: bytes


@dataclass(frozen=True, slots=True)
class _DeleteReplicaObject:
    key: str


@dataclass(frozen=True, slots=True)
class _ServeLogRange:
    doc_id: str
    writer: str
    after: int | None
    limit: int


@dataclass(frozen=True, slots=True)
class _ServeSyncPage:
    doc_ids: tuple[str, ...] | None
    after_key: str | None
    limit: int


@dataclass(frozen=True, slots=True)
class _ServeBlobChunk:
    digest: str
    offset: int
    length: int | None


@dataclass(frozen=True, slots=True)
class _ApplyRetention:
    doc_id: str


@dataclass(frozen=True, slots=True)
class _ApplyRetirement:
    record: tuple[tuple[str, object], ...]
    plan: tuple[tuple[str, object], ...]


@dataclass(frozen=True, slots=True)
class _ApplyBlobPurge:
    digests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ReplicaRead:
    name: str
    value: str | None = None


_ReplicaOperation = (
    _PrimeReplica
    | _ReconcileBlobs
    | _StoreObject
    | _DeleteReplicaObject
    | _ServeLogRange
    | _ServeSyncPage
    | _ServeBlobChunk
    | _ApplyRetention
    | _ApplyRetirement
    | _ApplyBlobPurge
    | _ReplicaRead
)


@dataclass(frozen=True, slots=True)
class _ReplicaCommand:
    operation_id: str
    generation: int
    operation: _ReplicaOperation


@dataclass(frozen=True, slots=True)
class _ReplicaFailure:
    kind: str
    code: str
    message: str
    details: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class _ReplicaOutcome:
    value: object = None
    error: _ReplicaFailure | None = None


@dataclass(slots=True)
class _ReplicaWaiter:
    done: threading.Event
    value: object = None
    error: _ReplicaFailure | None = None


class _ReplicaBackendView:
    """Compatibility backend seam routed through the replica authority."""

    def __init__(self, authority: "ReplicaAuthority") -> None:
        self._authority = authority

    def put(self, key: str, data: bytes) -> PutOutcome:
        return self._authority._ask(_StoreObject("backend_put", key, bytes(data)))

    def get(self, key: str) -> bytes | None:
        if getattr(self._authority._effect_context, "running", False):
            return self._authority._store.backend.get(key)
        return self._authority._ask(_ReplicaRead("backend_get", key))

    def exists(self, key: str) -> bool:
        if getattr(self._authority._effect_context, "running", False):
            return self._authority._store.backend.exists(key)
        return bool(self._authority._ask(_ReplicaRead("backend_exists", key)))

    def delete(self, key: str) -> None:
        self._authority._ask(_DeleteReplicaObject(key))

    def keys(self, prefix: str) -> Iterable[str]:
        return iter(self._authority._ask(_ReplicaRead("backend_keys", prefix)))

    def durable(self) -> bool:
        return self._authority.durable()


class ReplicaAuthority:
    """One bounded actor/effect lane that serializes one replica's lifecycle."""

    def __init__(
        self,
        store: ReplicaStore,
        source_store: LocalSpaceStore,
        blob_store: object | None,
    ) -> None:
        self._store = store
        self._source_store = source_store
        self._blob_store = blob_store
        self.space_id = store.space_id
        self._backend_view = _ReplicaBackendView(self)
        self.backend_kind = type(store.backend).__name__
        self._lock = threading.RLock()
        self._generation = 1
        self._closing = False
        self._closed = False
        self._effect_context = threading.local()
        self._waiters: dict[str, _ReplicaWaiter] = {}
        self._commands: dict[str, _ReplicaCommand] = {}
        self._submitted: set[str] = set()
        self._deferred: list[_ReplicaCommand] = []
        owner_ref = weakref.ref(self)

        def actor_event(event: ActorEvent) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._on_actor_event(event)

        def handle(command: object) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._on_command(command)

        def execute(command: _ReplicaCommand) -> _ReplicaOutcome:
            owner = owner_ref()
            if owner is None:
                return _ReplicaOutcome(
                    error=_ReplicaFailure("OrgFsError", "closed", "replica is closed")
                )
            return owner._execute(command.operation)

        self._runtime = ActorRuntime(event_sink=actor_event)
        self._actor: ActorHandle = self._runtime.start(
            ActorSpec(
                f"orgfs-replica-{self.space_id[:12]}",
                lambda: handle,
                mailbox_capacity=_REPLICA_CAPACITY,
                supervision_profile="state_authority",
            )
        )

        def complete(
            completion: EffectCompleted[_ReplicaOutcome],
        ) -> AdmissionResult:
            owner = owner_ref()
            if owner is None:
                return AdmissionResult.CLOSED
            return owner._runtime.tell(owner._actor, completion)

        self._effects: EffectLane[_ReplicaCommand, _ReplicaOutcome] = EffectLane(
            name=f"orgfs-replica-{self.space_id[:12]}-effects",
            execute=execute,
            complete=complete,
            capacity=_REPLICA_CAPACITY,
            workers=1,
        )

    @property
    def backend(self) -> _ReplicaBackendView:
        return self._backend_view

    @staticmethod
    def _freeze_value(value: object) -> object:
        if isinstance(value, Mapping):
            return tuple(sorted((str(key), ReplicaAuthority._freeze_value(item)) for key, item in value.items()))
        if isinstance(value, (tuple, list)):
            return tuple(ReplicaAuthority._freeze_value(item) for item in value)
        if is_dataclass(value):
            return tuple((field.name, ReplicaAuthority._freeze_value(getattr(value, field.name))) for field in fields(value))
        if value is None or isinstance(value, (str, bytes, int, float, bool)):
            return value
        return str(value)

    @staticmethod
    def _thaw_value(item: object) -> object:
        if isinstance(item, tuple) and all(
            isinstance(pair, tuple) and len(pair) == 2 and isinstance(pair[0], str)
            for pair in item
        ):
            return {key: ReplicaAuthority._thaw_value(child) for key, child in item}
        if isinstance(item, tuple):
            return tuple(ReplicaAuthority._thaw_value(child) for child in item)
        return item

    @staticmethod
    def _thaw_mapping(value: tuple[tuple[str, object], ...]) -> dict[str, object]:
        return {key: ReplicaAuthority._thaw_value(item) for key, item in value}

    def _execute(self, operation: _ReplicaOperation) -> _ReplicaOutcome:
        self._effect_context.running = True
        try:
            if isinstance(operation, _PrimeReplica):
                value = self._store.prime_from(self._source_store, self._blob_store)
            elif isinstance(operation, _ReconcileBlobs):
                value = self._store.reconcile_blobs(self._blob_store)
            elif isinstance(operation, _StoreObject):
                if operation.kind == "envelope":
                    value = self._store.store_envelope(operation.key, operation.data)
                elif operation.kind == "snapshot":
                    value = self._store.store_snapshot(operation.key, operation.data)
                elif operation.kind == "blob":
                    value = self._store.store_blob(operation.key, operation.data)
                elif operation.kind == "backend_put":
                    value = self._store.backend.put(operation.key, operation.data)
                else:
                    raise ValueError(f"unknown replica object operation {operation.kind}")
            elif isinstance(operation, _DeleteReplicaObject):
                value = self._store.backend.delete(operation.key)
            elif isinstance(operation, _ServeLogRange):
                value = self._store.serve_log_range(
                    operation.doc_id, operation.writer,
                    after=operation.after, limit=operation.limit,
                )
            elif isinstance(operation, _ServeSyncPage):
                value = self._store.serve_sync_page(
                    operation.doc_ids, after_key=operation.after_key, limit=operation.limit
                )
            elif isinstance(operation, _ServeBlobChunk):
                digest = _digest(operation.digest)
                if self._store.retirement_view.purge_listed(digest):
                    raise OrgFsError(
                        "purged", {"digest": digest, "spaceId": self.space_id}
                    )
                if type(operation.offset) is not int or operation.offset < 0:
                    raise OrgFsError("out-of-range", {"offset": operation.offset})
                if operation.length is not None and (
                    type(operation.length) is not int or operation.length <= 0
                ):
                    raise OrgFsError("out-of-range", {"length": operation.length})
                backend_override = vars(self._backend_view).get("get")
                raw = (
                    backend_override(f"blob/{digest}")
                    if backend_override is not None
                    else self._store.backend.get(f"blob/{digest}")
                )
                if raw is None:
                    raise OrgFsError("unknown-blob", {"digest": digest})
                if operation.offset > len(raw):
                    raise OrgFsError("out-of-range", {"offset": operation.offset})
                value = (
                    raw[operation.offset :]
                    if operation.length is None
                    else raw[operation.offset : operation.offset + operation.length]
                )
            elif isinstance(operation, _ApplyRetention):
                value = self._store.apply_retention(operation.doc_id)
            elif isinstance(operation, _ApplyRetirement):
                value = self._store.apply_retirement(
                    self._thaw_mapping(operation.record),
                    self._thaw_mapping(operation.plan),
                )
            elif isinstance(operation, _ApplyBlobPurge):
                value = self._store.apply_blob_purge(operation.digests)
            elif isinstance(operation, _ReplicaRead):
                if operation.name == "pinned_blobs":
                    value = self._store.pinned_blobs()
                elif operation.name == "durable":
                    value = self._store.durable()
                elif operation.name == "pinned_reason":
                    value = self._store.pinned_reason(operation.value or "")
                elif operation.name == "backend_get":
                    value = self._store.backend.get(operation.value or "")
                elif operation.name == "backend_exists":
                    value = self._store.backend.exists(operation.value or "")
                elif operation.name == "backend_keys":
                    value = tuple(self._store.backend.keys(operation.value or ""))
                else:
                    raise ValueError(f"unknown replica read operation {operation.name}")
            else:
                raise TypeError(f"unsupported replica operation {type(operation).__name__}")
            return _ReplicaOutcome(value=value)
        except Exception as error:
            details = getattr(error, "details", {})
            frozen = (
                tuple(sorted((str(key), str(value)) for key, value in dict(details).items()))
                if isinstance(details, Mapping)
                else ()
            )
            return _ReplicaOutcome(
                error=_ReplicaFailure(
                    type(error).__name__, str(getattr(error, "code", "internal")), str(error), frozen
                )
            )
        finally:
            self._effect_context.running = False

    def _on_command(self, command: object) -> None:
        if isinstance(command, EffectCompleted):
            with self._lock:
                current = self._commands.get(command.operation_id)
                if current is None or current.generation != command.generation:
                    waiter = None
                else:
                    self._commands.pop(command.operation_id, None)
                    self._submitted.discard(command.operation_id)
                    waiter = self._waiters.pop(command.operation_id, None)
            if waiter is not None:
                outcome = command.result
                if isinstance(outcome, _ReplicaOutcome):
                    waiter.value, waiter.error = outcome.value, outcome.error
                else:
                    waiter.error = _ReplicaFailure("RuntimeError", "internal", "invalid replica result")
                waiter.done.set()
            self._effects.acknowledge(command.operation_id, command.generation)
            self._pump_deferred()
            return
        if not isinstance(command, _ReplicaCommand):
            raise TypeError("replica authority received an invalid command")
        with self._lock:
            if command.generation != self._generation:
                return
            admission = self._effects.submit(
                EffectRequest(command.operation_id, command.generation, command)
            )
            if admission is AdmissionResult.ACCEPTED:
                self._deferred = [
                    item
                    for item in self._deferred
                    if item.operation_id != command.operation_id
                ]
                self._submitted.add(command.operation_id)
            elif admission is AdmissionResult.OVERLOADED:
                if all(
                    item.operation_id != command.operation_id
                    for item in self._deferred
                ):
                    self._deferred.append(command)

    def _pump_deferred(self) -> None:
        with self._lock:
            if not self._deferred:
                return
            command = self._deferred[0]
            admission = self._effects.submit(
                EffectRequest(command.operation_id, command.generation, command)
            )
            if admission is AdmissionResult.ACCEPTED:
                self._deferred.pop(0)
                self._submitted.add(command.operation_id)

    def _on_actor_event(self, event: ActorEvent) -> None:
        if event.kind is not ActorEventKind.CHILD_RESTARTED:
            return
        with self._lock:
            if event.generation <= self._generation:
                return
            self._generation = event.generation
            deferred_ids = {command.operation_id for command in self._deferred}
            replay = tuple(
                _ReplicaCommand(call.operation_id, event.generation, call.operation)
                for operation_id, call in self._commands.items()
                if operation_id not in self._submitted
            )
            for call in replay:
                self._commands[call.operation_id] = call
            replacements = {command.operation_id: command for command in replay}
            self._deferred = [
                replacements[command.operation_id]
                for command in self._deferred
                if command.operation_id in replacements
            ]
        self._pump_deferred()
        for call in replay:
            if call.operation_id in deferred_ids:
                continue
            admission = self._runtime.tell(self._actor, call)
            if admission is not AdmissionResult.ACCEPTED:
                with self._lock:
                    if all(
                        item.operation_id != call.operation_id
                        for item in self._deferred
                    ):
                        self._deferred.append(call)
                self._pump_deferred()

    def _ask(self, operation: _ReplicaOperation) -> Any:
        operation_id = uuid.uuid4().hex
        waiter = _ReplicaWaiter(threading.Event())
        with self._lock:
            if self._closing:
                raise OrgFsError("unavailable", {"message": "replica authority is closing"})
            if len(self._commands) >= _REPLICA_CAPACITY:
                raise OrgFsError(
                    "resource-exhausted",
                    {"message": "replica authority total custody is full"},
                )
            command = _ReplicaCommand(operation_id, self._generation, operation)
            self._commands[operation_id] = command
            self._waiters[operation_id] = waiter
        while True:
            admission = self._runtime.tell(self._actor, command)
            if admission is AdmissionResult.ACCEPTED:
                break
            with self._lock:
                current = self._commands.get(operation_id)
                if admission is AdmissionResult.CLOSED and current is not None and current.generation != command.generation:
                    command = current
                    continue
                self._commands.pop(operation_id, None)
                self._waiters.pop(operation_id, None)
            raise OrgFsError("resource-exhausted", {"message": "replica actor admission was rejected"})
        waiter.done.wait()
        if waiter.error is not None:
            error = waiter.error
            if error.kind == "OrgFsError":
                raise OrgFsError(error.code, {"message": error.message, **dict(error.details)})
            raise RuntimeError(error.message)
        return waiter.value

    def _freeze_object(self, value: object) -> tuple[tuple[str, object], ...]:
        if is_dataclass(value):
            value = {field.name: getattr(value, field.name) for field in fields(value)}
        if not isinstance(value, Mapping):
            return ()
        frozen = self._freeze_value(value)
        return frozen if isinstance(frozen, tuple) else ()

    def prime_from(self, _store: object, _blob_store: object | None = None) -> None:
        self._ask(_PrimeReplica())

    def reconcile_blobs(self) -> tuple[str, ...]:
        return self._ask(_ReconcileBlobs())

    def durable(self) -> bool:
        return bool(self._ask(_ReplicaRead("durable")))

    def pinned_blobs(self) -> tuple[str, ...]:
        return self._ask(_ReplicaRead("pinned_blobs"))

    def pinned_reason(self, digest: str) -> str | None:
        return self._ask(_ReplicaRead("pinned_reason", digest))

    def store_envelope(self, key: str, data: bytes) -> PutOutcome:
        return self._ask(_StoreObject("envelope", key, bytes(data))
        )

    def store_snapshot(self, key: str, data: bytes) -> PutOutcome:
        return self._ask(_StoreObject("snapshot", key, bytes(data)))

    def store_blob(self, digest: str, data: bytes) -> PutOutcome:
        return self._ask(_StoreObject("blob", digest, bytes(data)))

    def serve_log_range(self, doc_id: str, writer: str, *, after: int | None = None, limit: int = 256):
        return self._ask(_ServeLogRange(doc_id, writer, after, limit))

    def serve_sync_page(self, doc_ids: Iterable[str] | None = None, *, after_key: str | None = None, limit: int = 256):
        return self._ask(_ServeSyncPage(None if doc_ids is None else tuple(doc_ids), after_key, limit))

    def serve_blob_chunk(self, digest: str, *, offset: int = 0, length: int | None = None) -> bytes:
        return self._ask(_ServeBlobChunk(digest, offset, length))

    def apply_retention(self, doc_id: str) -> tuple[str, ...]:
        return self._ask(_ApplyRetention(doc_id))

    def apply_retirement(self, record: object, plan: object) -> tuple[str, ...]:
        return self._ask(_ApplyRetirement(self._freeze_object(record), self._freeze_object(plan)))

    def apply_blob_purge(self, digests: Iterable[str]) -> tuple[str, ...]:
        return self._ask(_ApplyBlobPurge(tuple(str(digest) for digest in digests)))

    def backend_get(self, key: str) -> bytes | None:
        return self._ask(_ReplicaRead("backend_get", key))

    def backend_exists(self, key: str) -> bool:
        return bool(self._ask(_ReplicaRead("backend_exists", key)))

    def backend_delete(self, key: str) -> None:
        self._ask(_DeleteReplicaObject(key))

    def backend_keys(self, prefix: str) -> tuple[str, ...]:
        return self._ask(_ReplicaRead("backend_keys", prefix))

    def close(self, timeout: float = 5.0) -> bool:
        with self._lock:
            if self._closed:
                return True
            self._closing = True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self._runtime.snapshot(self._actor)
            if snapshot.queued == 0 and snapshot.in_flight == 0 and not self._deferred:
                break
            time.sleep(0.005)
        else:
            return False
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        stopped = self._runtime.stop(
            self._actor, timeout=max(0.0, deadline - time.monotonic())
        )
        if stopped:
            self._closed = True
        return stopped
