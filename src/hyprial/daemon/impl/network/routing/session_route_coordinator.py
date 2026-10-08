"""Serialized Session lease and route effects with bounded actor admission."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest

from hyprial.daemon.impl.operations.session_ports  import (
    HeartbeatSessionCommand,
    RefreshSessionCommand,
    RegisterSessionCommand,
    UnregisterSessionCommand,
)


SessionCommand = (
    RegisterSessionCommand
    | RefreshSessionCommand
    | HeartbeatSessionCommand
    | UnregisterSessionCommand
)


class SessionRouteKind(StrEnum):
    REGISTER = "register"
    RETIRE = "retire"
    REFRESH = "refresh"
    HEARTBEAT = "heartbeat"
    UNREGISTER = "unregister"
    EXPIRE = "expire"
    ENSURE = "ensure"
    DROP = "drop"


@dataclass(frozen=True, slots=True)
class SessionRefRetirement:
    entity_token: str
    session_refs: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class SessionRouteRequest:
    operation_id: str
    epoch: int
    kind: SessionRouteKind
    actor: str = ""
    session_ref: str | None = None
    session_command: SessionCommand | None = None
    observed_at: float | None = None
    retirement: SessionRefRetirement | None = None


@dataclass(frozen=True, slots=True)
class SessionRouteCompleted:
    operation_id: str
    epoch: int
    result: object | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SessionRouteProjection:
    accepted: int
    overloaded: int
    completed: int
    failed: int
    pending: int
    closing: bool


class SessionRouteOverloaded(RuntimeError):
    pass


class _Waiter:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.completion: SessionRouteCompleted | None = None
        self.error: BaseException | None = None


class SessionRouteCoordinator:
    """Bounded per-resource progression for Session and route effects.

    Its actor owns admission and completion custody. A timed-out IPC waiter
    never cancels accepted work; late completion still retires the operation.
    Fixed domain effects run outside the mailbox. Requests sharing an actor or
    session fence stay FIFO; unrelated sessions may wait on their own Session
    completions concurrently.
    """

    def __init__(
        self,
        apply: Callable[[SessionRouteRequest], object],
        *,
        capacity: int = 32,
        effect_workers: int = 8,
        runtime: ActorRuntime | None = None,
    ) -> None:
        if capacity < 1 or effect_workers < 1:
            raise ValueError("capacity and effect_workers must be positive")
        self._apply = apply
        self._runtime = runtime or ActorRuntime()
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._epoch = 1
        self._capacity = capacity
        self._closing = False
        self._closed = False
        self._accepted = self._overloaded = self._completed = self._failed = 0
        self._pending: dict[str, _Waiter] = {}
        self._errors: dict[str, BaseException] = {}
        self._queued: deque[SessionRouteRequest] = deque()
        self._active: dict[str, frozenset[str]] = {}
        self._active_resources: set[str] = set()
        handle = None
        try:
            handle = self._runtime.start(
                ActorSpec(
                    name="session-route",
                    handler_factory=lambda: self._handle_command,
                    mailbox_capacity=capacity,
                    undelivered_sink=self._undelivered,
                )
            )
            self._handle = handle
            self._effects: EffectLane[
                SessionRouteRequest, SessionRouteCompleted
            ] = EffectLane(
                name="session-route-effects",
                execute=self._execute,
                complete=lambda event: self._runtime.tell(self._handle, event),
                capacity=capacity,
                workers=min(capacity, effect_workers),
            )
        except BaseException as error:
            # The application receives no coordinator to close when its
            # constructor fails. No request has been admitted yet.
            if handle is not None:
                try:
                    if not self._runtime.stop(handle, 5.0):
                        error.add_note("session-route actor did not stop during construction rollback")
                except BaseException as cleanup_error:
                    error.add_note(
                        "session-route actor rollback raised "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            raise

    def _undelivered(self, command: object, reason_code: str) -> None:
        if isinstance(command, SessionRouteRequest):
            self._settle(
                SessionRouteCompleted(
                    command.operation_id,
                    command.epoch,
                    error=reason_code,
                )
            )

    def request(
        self,
        kind: SessionRouteKind,
        *,
        actor: str = "",
        session_ref: str | None = None,
        session_command: SessionCommand | None = None,
        observed_at: float | None = None,
        retirement: SessionRefRetirement | None = None,
    ) -> tuple[AdmissionResult, str]:
        admission, operation_id, _waiter = self._admit(
            kind, actor=actor, session_ref=session_ref,
            session_command=session_command, observed_at=observed_at,
            retirement=retirement,
        )
        return admission, operation_id

    def _admit(
        self,
        kind: SessionRouteKind,
        *,
        actor: str = "",
        session_ref: str | None = None,
        session_command: SessionCommand | None = None,
        observed_at: float | None = None,
        retirement: SessionRefRetirement | None = None,
    ) -> tuple[AdmissionResult, str, _Waiter | None]:
        with self._lock:
            if self._closing:
                return AdmissionResult.CLOSED, "", None
            if len(self._pending) >= self._capacity:
                self._overloaded += 1
                return AdmissionResult.OVERLOADED, "", None
            operation_id = uuid.uuid4().hex
            waiter = _Waiter()
            self._pending[operation_id] = waiter
            self._idle.clear()
            command = SessionRouteRequest(
                operation_id, self._epoch, kind, actor, session_ref,
                session_command, observed_at, retirement,
            )
            admission = self._runtime.tell(self._handle, command)
            if admission is AdmissionResult.ACCEPTED:
                self._accepted += 1
            else:
                self._pending.pop(operation_id)
                self._overloaded += admission is AdmissionResult.OVERLOADED
                if not self._pending:
                    self._idle.set()
            return (
                admission,
                operation_id if admission is AdmissionResult.ACCEPTED else "",
                waiter if admission is AdmissionResult.ACCEPTED else None,
            )

    def call(self, kind: SessionRouteKind, *, timeout: float = 65.0, **kwargs: object) -> object:
        admission, operation_id, waiter = self._admit(kind, **kwargs)  # type: ignore[arg-type]
        if admission is not AdmissionResult.ACCEPTED:
            raise SessionRouteOverloaded(f"session route admission is {admission.value}")
        assert waiter is not None
        if not waiter.done.wait(max(0.0, timeout)):
            raise TimeoutError(f"session route {operation_id} accepted but still pending")
        assert waiter.completion is not None
        if waiter.error is not None:
            raise waiter.error
        if waiter.completion.error is not None:
            raise RuntimeError(waiter.completion.error)
        return waiter.completion.result

    def projection(self) -> SessionRouteProjection:
        with self._lock:
            return SessionRouteProjection(
                self._accepted, self._overloaded, self._completed,
                self._failed, len(self._pending), self._closing,
            )

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            if self._closed:
                return True
            self._closing = True
        if not self._idle.wait(max(0.0, deadline - time.monotonic())):
            return False
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        stopped = self._runtime.stop(
            self._handle, max(0.0, deadline - time.monotonic())
        )
        with self._lock:
            self._closed = stopped
            if stopped:
                self._epoch += 1
        return stopped

    def _handle_command(self, command: object) -> None:
        if isinstance(command, SessionRouteRequest):
            self._queued.append(command)
            self._pump()
            return
        if isinstance(command, EffectCompleted):
            if command.generation != self._epoch:
                self._effects.acknowledge(command.operation_id, command.generation)
                return
            completion = command.result or SessionRouteCompleted(
                command.operation_id, command.generation,
                error=command.error or "SessionRouteEffectError",
            )
            self._settle(completion)
            self._effects.acknowledge(command.operation_id, command.generation)
            resources = self._active.pop(command.operation_id, frozenset())
            self._active_resources.difference_update(resources)
            self._pump()
            return
        raise TypeError(f"unexpected session route command {type(command).__name__}")

    def _pump(self) -> None:
        if not self._queued:
            return
        retained: deque[SessionRouteRequest] = deque()
        blocked_resources: set[str] = set()
        while self._queued:
            request = self._queued.popleft()
            resources = _request_resources(request)
            if resources & (self._active_resources | blocked_resources):
                retained.append(request)
                blocked_resources.update(resources)
                continue
            admitted = self._effects.submit(
                EffectRequest(request.operation_id, request.epoch, request)
            )
            if admitted is AdmissionResult.ACCEPTED:
                self._active[request.operation_id] = resources
                self._active_resources.update(resources)
                continue
            if admitted is AdmissionResult.OVERLOADED:
                retained.append(request)
                retained.extend(self._queued)
                self._queued.clear()
                break
            with self._lock:
                self._errors[request.operation_id] = SessionRouteOverloaded(
                    "session route effect lane closed"
                )
            self._settle(
                SessionRouteCompleted(
                    request.operation_id,
                    request.epoch,
                    error="SessionRouteOverloaded",
                )
            )
        self._queued = retained

    def _settle(self, completion: SessionRouteCompleted) -> None:
        with self._lock:
            if completion.epoch != self._epoch:
                return
            waiter = self._pending.pop(completion.operation_id, None)
            if waiter is None:
                return
            self._completed += 1
            self._failed += completion.error is not None
            waiter.error = self._errors.pop(completion.operation_id, None)
            waiter.completion = completion
            waiter.done.set()
            if not self._pending:
                self._idle.set()

    def _execute(self, request: SessionRouteRequest) -> SessionRouteCompleted:
        try:
            result = self._apply(request)
            return SessionRouteCompleted(request.operation_id, request.epoch, result)
        except BaseException as error:
            # Exceptions never enter typed completion messages. Their exact
            # instance remains in local custody for the waiting system edge.
            with self._lock:
                self._errors[request.operation_id] = error
            return SessionRouteCompleted(
                request.operation_id, request.epoch, error=type(error).__name__
            )


def _request_resources(request: SessionRouteRequest) -> frozenset[str]:
    if request.kind is SessionRouteKind.EXPIRE:
        return frozenset(("expiry",))
    resources = {f"actor:{request.actor}"}
    if request.session_ref is not None:
        resources.add(f"session:{request.session_ref}")
    return frozenset(resources)
