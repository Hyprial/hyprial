"""One bounded authority for blob references shared by every orgfs space.

The ActorRuntime child owns command admission. A single bounded effect worker
owns the underlying BlobStore connection and filesystem operations, keeping SQL
and fsync away from transport callbacks and facade transactions.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time
import uuid
import weakref
from typing import Any, Mapping

from hyprial.kernel import (
    ActorEvent,
    ActorEventKind,
    ActorHandle,
    ActorRuntime,
    ActorSpec,
    AdmissionResult)
from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest

from hyprial.daemon.impl.orgfs.storage.blobs  import (
    BlobError,
    BlobIntegrityError,
    BlobMissing,
    BlobPayload,
    BlobPurged,
    BlobRangeError,
    BlobStore)


_BLOB_QUEUE_CAPACITY = 64
_BLOB_CALL_TIMEOUT = 60.0
@dataclass(frozen=True, slots=True)
class _PutBlob:
    space_id: str
    data: bytes
    reason: str


@dataclass(frozen=True, slots=True)
class _GetBlob:
    space_id: str
    digest: str


@dataclass(frozen=True, slots=True)
class _HasBlob:
    digest: str


@dataclass(frozen=True, slots=True)
class _ContainsBlob:
    digest: str


@dataclass(frozen=True, slots=True)
class _PinBlob:
    space_id: str
    digest: str
    reason: str


@dataclass(frozen=True, slots=True)
class _ListBlobReferences:
    space_id: str | None
    digest: str | None


@dataclass(frozen=True, slots=True)
class _FindOtherBlobReferences:
    space_id: str
    digest: str


@dataclass(frozen=True, slots=True)
class _ReleaseBlob:
    space_id: str
    digest: str


@dataclass(frozen=True, slots=True)
class _DeleteBlobIfUnreferenced:
    digest: str


@dataclass(frozen=True, slots=True)
class _ReadBlobChunk:
    request: bytes


@dataclass(frozen=True, slots=True)
class _ReadBlobChunkAlias:
    request: bytes


@dataclass(frozen=True, slots=True)
class _HandleBlobRequest:
    request: bytes


_BlobOperation = (
    _PutBlob
    | _GetBlob
    | _HasBlob
    | _ContainsBlob
    | _PinBlob
    | _ListBlobReferences
    | _FindOtherBlobReferences
    | _ReleaseBlob
    | _DeleteBlobIfUnreferenced
    | _ReadBlobChunk
    | _ReadBlobChunkAlias
    | _HandleBlobRequest
)


@dataclass(frozen=True, slots=True)
class _BlobCommand:
    operation_id: str
    generation: int
    operation: _BlobOperation


@dataclass(frozen=True, slots=True)
class _BlobFailure:
    error_type: str
    message: str
    code: str | None = None
    details: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class _BlobResult:
    operation_id: str
    generation: int
    result: object = None
    error: _BlobFailure | None = None


@dataclass(frozen=True, slots=True)
class _BlobExecution:
    value: object = None
    error: _BlobFailure | None = None


@dataclass(slots=True)
class _Waiter:
    done: threading.Event
    result: object = None
    error: _BlobFailure | None = None


class BlobAuthority:
    """Shared blob facade with bounded admission and one storage owner."""

    def __init__(self, state_root: Path | str) -> None:
        self._store = BlobStore(state_root)
        self.state_root = self._store.state_root
        self.blob_root = self._store.blob_root
        self._waiters: dict[str, _Waiter] = {}
        self._calls: dict[str, _BlobCommand] = {}
        self._submitted: set[str] = set()
        self._lock = threading.Lock()
        self._drained = threading.Condition(self._lock)
        self._closing = False
        self._closed = False
        self._generation = 1
        owner_ref = weakref.ref(self)

        def actor_event(event: ActorEvent) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._on_actor_event(event)

        self._runtime = ActorRuntime(event_sink=actor_event)

        def handle_command(command: object) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._on_command(command)

        def execute_call(call: _BlobCommand) -> _BlobExecution:
            owner = owner_ref()
            return (
                owner._execute(call)
                if owner is not None
                else _BlobExecution(error=BlobError("blob authority is closed"))
            )

        def complete_call(
            completion: EffectCompleted[_BlobExecution],
        ) -> AdmissionResult:
            owner = owner_ref()
            return (
                owner._complete_effect(completion)
                if owner is not None
                else AdmissionResult.CLOSED
            )

        self._handle: ActorHandle = self._runtime.start(
            ActorSpec(
                "orgfs-blob-authority",
                lambda: handle_command,
                mailbox_capacity=_BLOB_QUEUE_CAPACITY,
                supervision_profile="state_authority",
            )
        )
        self._effects: EffectLane[_BlobCommand, _BlobExecution] = EffectLane(
            name="orgfs-blob-effects",
            execute=execute_call,
            complete=complete_call,
            capacity=_BLOB_QUEUE_CAPACITY,
            workers=1,
        )

    def _on_command(self, command: object) -> None:
        if isinstance(command, _BlobCommand):
            with self._lock:
                if command.generation != self._generation:
                    return
            admitted = self._effects.submit(
                EffectRequest(command.operation_id, command.generation, command)
            )
            if admitted is not AdmissionResult.ACCEPTED:
                self._complete(
                    _BlobResult(
                        command.operation_id,
                        command.generation,
                        error=_BlobFailure(
                            "BlobError",
                            "blob authority effect lane is "
                            + ("full" if admitted is AdmissionResult.OVERLOADED else "closed"),
                        ),
                    )
                )
            else:
                with self._lock:
                    self._submitted.add(command.operation_id)
            return
        if isinstance(command, _BlobResult):
            self._complete(command)
            self._effects.acknowledge(command.operation_id, command.generation)
            return
        raise TypeError(f"unsupported blob authority command: {type(command).__name__}")

    def _complete(self, result: _BlobResult) -> None:
        with self._lock:
            call = self._calls.get(result.operation_id)
            if call is None or result.generation != call.generation:
                return
            waiter = self._waiters.pop(result.operation_id, None)
            self._calls.pop(result.operation_id, None)
            self._submitted.discard(result.operation_id)
            self._drained.notify_all()
        if waiter is None:
            return
        waiter.result = result.result
        waiter.error = result.error
        waiter.done.set()

    def _on_actor_event(self, event: ActorEvent) -> None:
        if (
            event.handle.name != "orgfs-blob-authority"
            or event.kind is not ActorEventKind.CHILD_RESTARTED
        ):
            return
        with self._lock:
            if event.generation <= self._generation:
                return
            self._generation = event.generation
            replay = tuple(
                _BlobCommand(
                    call.operation_id,
                    event.generation,
                    call.operation,
                )
                for operation_id, call in self._calls.items()
                if operation_id not in self._submitted
            )
            for call in replay:
                self._calls[call.operation_id] = call
        # Only commands still in the old authority mailbox are replayed.
        # Commands already admitted to the effect lane retain their original
        # token and completion; their I/O is never repeated after a restart.
        for call in replay:
            if self._runtime.tell(self._handle, call) is AdmissionResult.CLOSED:
                return

    def _execute(self, call: _BlobCommand) -> _BlobExecution:
        try:
            operation = call.operation
            if isinstance(operation, _PutBlob):
                value = self._store.put(
                    operation.space_id, operation.data, reason=operation.reason
                )
            elif isinstance(operation, _GetBlob):
                value = self._store.get(operation.space_id, operation.digest)
            elif isinstance(operation, _HasBlob):
                value = self._store.has(operation.digest)
            elif isinstance(operation, _ContainsBlob):
                value = self._store.contains(operation.digest)
            elif isinstance(operation, _PinBlob):
                value = self._store.pin(
                    operation.space_id, operation.digest, operation.reason
                )
            elif isinstance(operation, _ListBlobReferences):
                value = self._store.references(operation.space_id, operation.digest)
            elif isinstance(operation, _FindOtherBlobReferences):
                value = self._store.referenced_elsewhere(
                    operation.space_id, operation.digest
                )
            elif isinstance(operation, _ReleaseBlob):
                value = self._store.release(operation.space_id, operation.digest)
            elif isinstance(operation, _DeleteBlobIfUnreferenced):
                value = self._store.delete_if_unreferenced(operation.digest)
            elif isinstance(operation, _ReadBlobChunk):
                value = self._store.get_chunk(operation.request)
            elif isinstance(operation, _ReadBlobChunkAlias):
                value = self._store.chunk(operation.request)
            elif isinstance(operation, _HandleBlobRequest):
                value = self._store.handle_request(operation.request)
            else:
                raise TypeError(f"unsupported blob command: {type(operation).__name__}")
        except BaseException as error:
            return _BlobExecution(error=self._freeze_error(error))
        return _BlobExecution(value=value)

    @staticmethod
    def _freeze_error(error: BaseException) -> _BlobFailure:
        code = getattr(error, "code", None)
        details = getattr(error, "details", {})
        frozen = (
            tuple(
                sorted(
                    (str(key), BlobAuthority._freeze_value(value))
                    for key, value in dict(details).items()
                )
            )
            if isinstance(details, Mapping)
            else ()
        )
        return _BlobFailure(type(error).__name__, str(error), code, frozen)

    @staticmethod
    def _freeze_value(value: object) -> object:
        if isinstance(value, Mapping):
            return tuple(
                sorted(
                    (str(key), BlobAuthority._freeze_value(item))
                    for key, item in value.items()
                )
            )
        if isinstance(value, (list, tuple)):
            return tuple(BlobAuthority._freeze_value(item) for item in value)
        if isinstance(value, (set, frozenset)):
            return tuple(sorted(BlobAuthority._freeze_value(item) for item in value))
        if value is None or isinstance(value, (str, int, float, bool, bytes)):
            return value
        return str(value)

    @staticmethod
    def _restore_error(error: _BlobFailure) -> BaseException:
        details = dict(error.details)
        if error.error_type == "BlobMissing":
            return BlobMissing(str(details.get("digest", "")))
        if error.error_type == "BlobPurged":
            return BlobPurged(str(details.get("digest", "")))
        if error.error_type == "BlobRangeError":
            return BlobRangeError(error.message, details=details)
        if error.error_type == "BlobIntegrityError":
            return BlobIntegrityError(error.message, details=details)
        if error.error_type == "ValueError":
            return ValueError(error.message)
        if error.error_type == "FileNotFoundError":
            return FileNotFoundError(error.message)
        return BlobError(error.message, details=details)

    def _complete_effect(
        self, completion: EffectCompleted[_BlobExecution]
    ) -> AdmissionResult:
        execution = completion.result
        return self._runtime.tell(
            self._handle,
            _BlobResult(
                completion.operation_id,
                completion.generation,
                result=execution.value if execution is not None else None,
                error=(
                    execution.error
                    if execution is not None and execution.error is not None
                    else (
                        _BlobFailure(
                            "BlobError",
                            "blob effect failed",
                            details=(("kind", completion.error),),
                        )
                        if completion.error is not None
                        else None
                    )
                ),
            ),
        )

    def _call(self, operation: _BlobOperation) -> Any:
        call_id = uuid.uuid4().hex
        waiter = _Waiter(threading.Event())
        with self._lock:
            if self._closing or self._closed:
                raise BlobError("blob authority is closed")
            self._waiters[call_id] = waiter
            generation = self._generation
            command = _BlobCommand(call_id, generation, operation)
            self._calls[call_id] = command
        while True:
            admitted = self._runtime.tell(self._handle, command)
            if admitted is AdmissionResult.ACCEPTED:
                break
            with self._lock:
                current = self._calls.get(call_id)
                if (
                    admitted is AdmissionResult.CLOSED
                    and current is not None
                    and current.generation != command.generation
                ):
                    command = current
                    continue
                self._waiters.pop(call_id, None)
                self._calls.pop(call_id, None)
            if admitted is AdmissionResult.OVERLOADED:
                raise BlobError("blob authority mailbox is full")
            raise BlobError("blob authority is closed")
        if not waiter.done.wait(_BLOB_CALL_TIMEOUT):
            # Timeout reports only that this caller stopped waiting. The
            # accepted operation remains in custody until its completion.
            raise BlobError(
                "blob operation is still in progress",
                details={"operationId": call_id},
            )
        if waiter.error is not None:
            raise self._restore_error(waiter.error)
        return waiter.result

    def put(self, space_id: str, data: BlobPayload, *, reason: str) -> str:
        return self._call(_PutBlob(space_id, bytes(data), reason))

    def get(self, space_id: str, digest: str) -> bytes:
        return self._call(_GetBlob(space_id, digest))

    def _path(self, digest: str) -> Path:
        """Compatibility path view for local integrity diagnostics."""

        return self._store._path(digest)

    def has(self, digest: str) -> bool:
        return self._call(_HasBlob(digest))

    def contains(self, digest: str) -> bool:
        return self._call(_ContainsBlob(digest))

    def pin(self, space_id: str, digest: str, reason: str) -> None:
        self._call(_PinBlob(space_id, digest, reason))

    def references(
        self, space_id: str | None = None, digest: str | None = None
    ) -> tuple[tuple[str, str, str], ...]:
        return self._call(_ListBlobReferences(space_id, digest))

    def referenced_elsewhere(self, space_id: str, digest: str) -> tuple[str, ...]:
        return self._call(_FindOtherBlobReferences(space_id, digest))

    def release(self, space_id: str, digest: str) -> None:
        self._call(_ReleaseBlob(space_id, digest))

    def delete_if_unreferenced(self, digest: str) -> bool:
        return self._call(_DeleteBlobIfUnreferenced(digest))

    @staticmethod
    def _freeze_request(request: bytes | Mapping[str, Any]) -> bytes:
        if isinstance(request, Mapping):
            return json.dumps(
                dict(request), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        return bytes(request)

    def get_chunk(self, request: bytes | Mapping[str, Any]) -> bytes:
        return self._call(_ReadBlobChunk(self._freeze_request(request)))

    def chunk(self, request: bytes | Mapping[str, Any]) -> bytes:
        return self._call(_ReadBlobChunkAlias(self._freeze_request(request)))

    def handle_request(self, request: bytes | Mapping[str, Any]) -> bytes:
        return self._call(_HandleBlobRequest(self._freeze_request(request)))

    def register_purge_checker(self, space_id: str, checker: Any) -> None:
        # This is immutable callback configuration, not a DB operation. Keep
        # it outside mailbox payloads and invoke it without holding its map lock.
        with self._lock:
            if self._closing or self._closed:
                raise BlobError("blob authority is closed")
        self._store.register_purge_checker(space_id, checker)

    def register_purge_projection(
        self, space_id: str, digests: tuple[str, ...] | frozenset[str]
    ) -> None:
        """Install a versioned immutable purge projection for one space."""

        with self._lock:
            if self._closing or self._closed:
                raise BlobError("blob authority is closed")
        self._store.register_purge_projection(space_id, digests)

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            if self._closed:
                return True
            self._closing = True
        with self._drained:
            while self._waiters:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._drained.wait(remaining)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        if not self._runtime.stop(
            self._handle, timeout=max(0.0, deadline - time.monotonic())
        ):
            return False
        self._store.close()
        with self._lock:
            self._generation += 1
            self._closed = True
        return True

    def __enter__(self) -> "BlobAuthority":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
