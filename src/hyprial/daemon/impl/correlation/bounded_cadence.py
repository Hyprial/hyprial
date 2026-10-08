"""Bounded actor admission for periodic domain effects."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest

_Result = TypeVar("_Result")


@dataclass(frozen=True, slots=True)
class CadenceTick:
    operation_id: str
    epoch: int
    observed_at_ms: int


@dataclass(frozen=True, slots=True)
class CadenceCompleted(Generic[_Result]):
    epoch: int
    observed_at_ms: int
    started_at: float
    duration_ms: int
    result: _Result | None
    error: str | None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class CadenceProjection:
    accepted: int
    overloaded: int
    coalesced: int
    completed: int
    failed: int
    active: bool
    closing: bool
    last_completed_at_ms: int | None


class BoundedCadence(Generic[_Result]):
    """Coalesce ticks while a fixed effect runs; retain it through owner ACK."""

    def __init__(
        self,
        name: str,
        effect: Callable[[int], _Result],
        completed: Callable[[CadenceCompleted[_Result]], None] | None = None,
        *,
        mailbox_capacity: int = 8,
        runtime: ActorRuntime | None = None,
    ) -> None:
        self._effect = effect
        self._completed = completed
        self._runtime = runtime or ActorRuntime()
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._epoch = 1
        self._closing = self._closed = False
        self._active = False
        self._active_operation_id: str | None = None
        self._admitted_unhandled = 0
        self._deferred: CadenceTick | None = None
        self._accepted = self._overloaded = self._coalesced = 0
        self._completed_count = self._failed = 0
        self._last_completed_at_ms: int | None = None
        self._handle = self._runtime.start(
            ActorSpec(
                name=name,
                handler_factory=lambda: self._handle_command,
                mailbox_capacity=mailbox_capacity,
            )
        )
        self._effects: EffectLane[CadenceTick, CadenceCompleted[_Result]] = EffectLane(
            name=f"{name}-effect",
            execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=1,
        )

    def submit(self, observed_at_ms: int) -> AdmissionResult:
        with self._lock:
            if self._closing:
                return AdmissionResult.CLOSED
            tick = CadenceTick(uuid.uuid4().hex, self._epoch, observed_at_ms)
            admitted = self._runtime.tell(self._handle, tick)
            if admitted is AdmissionResult.ACCEPTED:
                self._accepted += 1
                self._admitted_unhandled += 1
                self._idle.clear()
            elif admitted is AdmissionResult.OVERLOADED:
                self._overloaded += 1
            return admitted

    def projection(self) -> CadenceProjection:
        with self._lock:
            return CadenceProjection(
                self._accepted, self._overloaded, self._coalesced,
                self._completed_count, self._failed, self._active, self._closing,
                self._last_completed_at_ms,
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
        if isinstance(command, CadenceTick):
            with self._lock:
                self._admitted_unhandled -= 1
                if command.epoch != self._epoch:
                    return
                if self._active:
                    self._deferred = command
                    self._coalesced += 1
                    return
                self._active = True
            self._submit_effect(command)
            return
        if not isinstance(command, EffectCompleted):
            raise TypeError(f"unexpected cadence command: {type(command).__name__}")
        if command.generation != self._epoch:
            self._effects.acknowledge(command.operation_id, command.generation)
            return
        with self._lock:
            if command.operation_id != self._active_operation_id:
                self._effects.acknowledge(command.operation_id, command.generation)
                return
        result = command.result
        next_tick = self._settle(
            result.observed_at_ms if result is not None else 0,
            failed=bool(command.error or (result is not None and result.error)),
        )
        # The worker keeps retrying this completion until the actor has
        # changed its projection and acknowledges the exact operation.
        self._effects.acknowledge(command.operation_id, command.generation)
        if next_tick is not None:
            self._submit_effect(next_tick)

    def _submit_effect(self, tick: CadenceTick) -> None:
        with self._lock:
            self._active_operation_id = tick.operation_id
        admitted = self._effects.submit(
            EffectRequest(tick.operation_id, tick.epoch, tick)
        )
        if admitted is not AdmissionResult.ACCEPTED:
            next_tick = self._settle(tick.observed_at_ms, failed=True)
            if next_tick is not None:
                self._submit_effect(next_tick)

    def _settle(self, observed_at_ms: int, *, failed: bool) -> CadenceTick | None:
        with self._lock:
            self._completed_count += 1
            self._failed += failed
            self._last_completed_at_ms = observed_at_ms
            next_tick = self._deferred
            self._deferred = None
            self._active_operation_id = None
            if next_tick is None:
                self._active = False
                if self._admitted_unhandled == 0:
                    self._idle.set()
            return next_tick

    def _execute(self, tick: CadenceTick) -> CadenceCompleted[_Result]:
        started = time.monotonic()
        try:
            result = self._effect(tick.observed_at_ms)
            error = None
            detail = None
        except Exception as caught:
            result = None
            error = type(caught).__name__
            detail = str(caught)[:500]
        completion = CadenceCompleted(
            tick.epoch, tick.observed_at_ms, started,
            int((time.monotonic() - started) * 1000), result, error, detail,
        )
        if self._completed is not None:
            try:
                self._completed(completion)
            except Exception:
                completion = CadenceCompleted(
                    tick.epoch, tick.observed_at_ms, started,
                    completion.duration_ms, result, "CompletionReporterError",
                    "completion reporter failed",
                )
        return completion
