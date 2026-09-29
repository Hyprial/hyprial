"""Bounded actor ownership of local IPC request lifetimes."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.actor_runtime.effects import EffectCompleted, EffectLane, EffectRequest


@dataclass(frozen=True, slots=True)
class RequestStarted:
    operation_id: str
    generation: int
    method: str
    started_at: float


@dataclass(frozen=True, slots=True)
class RequestCompleted:
    operation_id: str
    generation: int
    error_code: str | None
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class RequestProjection:
    accepted: int
    overloaded: int
    completed: int
    failed: int
    active: int
    closing: bool


class IpcRequestOwner:
    """Observe each IPC request through domain settlement, including late work.

    Business methods run on their domain boundaries, outside this mailbox.
    The completion lane retains accepted results until the actor processes and
    acknowledges them, including across a transient actor restart.
    """

    def __init__(self, *, capacity: int = 64, runtime: ActorRuntime | None = None):
        self._runtime = runtime or ActorRuntime()
        self._guard = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._generation = 1
        self._closing = self._closed = False
        self._active: dict[str, RequestStarted] = {}
        self._accepted = self._overloaded = self._completed = self._failed = 0
        self._handle = self._runtime.start(
            ActorSpec(
                name="ipc-requests",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity * 2,
            )
        )
        self._effects: EffectLane[RequestCompleted, RequestCompleted] = EffectLane(
            name="ipc-completions",
            execute=lambda completed: completed,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity,
        )

    def start(self, method: str) -> RequestStarted | None:
        command = RequestStarted(
            uuid.uuid4().hex, self._generation, method, time.monotonic()
        )
        with self._guard:
            if self._closing:
                return None
            result = self._runtime.tell(self._handle, command)
            if result is AdmissionResult.ACCEPTED:
                self._accepted += 1
                self._idle.clear()
                return command
            self._overloaded += 1
            return None

    def complete(self, started: RequestStarted, error_code: str | None) -> AdmissionResult:
        completed = RequestCompleted(
            started.operation_id,
            started.generation,
            error_code,
            int((time.monotonic() - started.started_at) * 1000),
        )
        return self._effects.submit(
            EffectRequest(started.operation_id, started.generation, completed)
        )

    def projection(self) -> RequestProjection:
        with self._guard:
            return RequestProjection(
                self._accepted, self._overloaded, self._completed,
                self._failed, len(self._active), self._closing,
            )

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
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
        with self._guard:
            self._closed = stopped
        return stopped

    def _receive(self, command: object) -> None:
        if isinstance(command, RequestStarted):
            with self._guard:
                self._active[command.operation_id] = command
            return
        if isinstance(command, EffectCompleted):
            result = command.result
            with self._guard:
                if (
                    result is not None
                    and result.generation == self._generation
                    and self._active.pop(result.operation_id, None) is not None
                ):
                    self._completed += 1
                    self._failed += result.error_code is not None
                    if not self._active:
                        self._idle.set()
            self._effects.acknowledge(command.operation_id, command.generation)
            return
        raise TypeError(f"unsupported IPC request message {type(command).__name__}")
