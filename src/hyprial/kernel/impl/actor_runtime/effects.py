"""Bounded effect execution with actor-acknowledged completion custody.

The handler is a fixed domain dependency. Requests contain immutable domain
values, never caller-provided executable closures. Admission is not settlement;
capacity stays reserved until the owner acknowledges the completion token.
"""

from __future__ import annotations

import collections
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from hyprial.kernel.impl.actor_runtime.contracts  import AdmissionResult
from hyprial.kernel.impl.facts.cost_counters import (
    RuntimeCpuCounters,
    runtime_cpu_counters,
)

RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True)
class EffectRequest(Generic[RequestT]):
    operation_id: str
    generation: int
    payload: RequestT


@dataclass(frozen=True, slots=True)
class EffectCompleted(Generic[ResultT]):
    operation_id: str
    generation: int
    result: ResultT | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class EffectLaneSnapshot:
    outstanding: int
    rejected: int
    completion_retries: int
    closed: bool


class EffectLane(Generic[RequestT, ResultT]):
    def __init__(
        self,
        *,
        name: str,
        execute: Callable[[RequestT], ResultT],
        complete: Callable[[EffectCompleted[ResultT]], AdmissionResult],
        capacity: int = 32,
        workers: int = 1,
        retry_seconds: float = 0.01,
        cpu_accounting: RuntimeCpuCounters = runtime_cpu_counters,
    ) -> None:
        if capacity < 1 or workers < 1 or retry_seconds <= 0:
            raise ValueError(
                "effect capacity, workers and retry interval must be positive"
            )
        self._execute = execute
        self._complete = complete
        self._capacity = capacity
        self._retry = retry_seconds
        self._cpu_accounting = cpu_accounting
        self._cpu_owner = cpu_accounting.owner(name)
        # Reservations bound capacity; the condition owns queue access.
        # Idle workers park until submission or custody settlement wakes them.
        self._requests: collections.deque[EffectRequest[RequestT]] = collections.deque()
        self._condition = threading.Condition()
        self._pending: dict[tuple[str, int], threading.Event] = {}
        self._submitted: set[tuple[str, int]] = set()
        self._closed = False
        self._rejected = self._retries = 0
        self._threads = tuple(
            threading.Thread(target=self._run, name=f"{name}-{i}", daemon=True)
            for i in range(workers)
        )
        for thread in self._threads:
            thread.start()

    def submit(self, request: EffectRequest[RequestT]) -> AdmissionResult:
        result = self.reserve(request.operation_id, request.generation)
        if result is not AdmissionResult.ACCEPTED:
            return result
        return self.submit_reserved(request)

    def reserve(self, operation_id: str, generation: int) -> AdmissionResult:
        """Reserve capacity before a durable commit creates its effect intent."""
        token = (operation_id, generation)
        with self._condition:
            if self._closed:
                self._rejected += 1
                return AdmissionResult.CLOSED
            if token in self._pending:
                return AdmissionResult.ACCEPTED
            if len(self._pending) >= self._capacity:
                self._rejected += 1
                return AdmissionResult.OVERLOADED
            self._pending[token] = threading.Event()
            return AdmissionResult.ACCEPTED

    def submit_reserved(self, request: EffectRequest[RequestT]) -> AdmissionResult:
        """Complete admitted custody, even after close stops new reservations.

        This cannot overflow: the reservation already owns a capacity slot.
        Callers cancel a reservation only when their durable commit failed.
        """
        token = (request.operation_id, request.generation)
        with self._condition:
            if token not in self._pending:
                self._rejected += 1
                return AdmissionResult.CLOSED
            if token in self._submitted:
                return AdmissionResult.ACCEPTED
            self._submitted.add(token)
            self._requests.append(request)
            self._condition.notify_all()
            return AdmissionResult.ACCEPTED

    def cancel_reservation(self, operation_id: str, generation: int) -> bool:
        token = (operation_id, generation)
        with self._condition:
            if token in self._submitted:
                return False
            receipt = self._pending.pop(token, None)
            if receipt is None:
                return False
            receipt.set()
            self._condition.notify_all()
            return True

    def acknowledge(self, operation_id: str, generation: int) -> bool:
        """Owner calls only after handling the completion, including rejection
        of stale generations. Never acknowledge merely after mailbox admission.
        """
        with self._condition:
            token = (operation_id, generation)
            if token not in self._submitted:
                return False
            receipt = self._pending.pop(token, None)
            if receipt is None:
                return False
            receipt.set()
            self._submitted.discard(token)
            self._condition.notify_all()
            return True

    def _take_request(self) -> EffectRequest[RequestT] | None:
        with self._condition:
            while not self._requests:
                if self._closed and not self._pending:
                    return None
                self._condition.wait()
            return self._requests.popleft()

    def _run(self) -> None:
        while True:
            request = self._take_request()
            if request is None:
                return
            token = (request.operation_id, request.generation)
            with self._condition:
                receipt = self._pending[token]
            try:
                costs = self._cpu_accounting
                owner = self._cpu_owner
                account = owner is not None and costs.enabled
                started_cpu = (
                    time.thread_time() if account else 0.0
                )
                try:
                    result = self._execute(request.payload)
                finally:
                    if account and owner is not None:
                        try:
                            costs.record_normalized(
                                owner,
                                type(request.payload).__name__,
                                cpu_seconds=time.thread_time() - started_cpu,
                            )
                        except Exception:  # noqa: BLE001 - accounting never changes the outcome
                            pass
                event = EffectCompleted(
                    request.operation_id, request.generation, result
                )
            except BaseException as error:
                # Native adapters may raise SystemExit/KeyboardInterrupt on
                # their own worker. Accepted custody must still reach the owner.
                event = EffectCompleted(
                    request.operation_id, request.generation, error=type(error).__name__
                )
            while not receipt.is_set():
                try:
                    self._complete(event)
                except BaseException:
                    pass  # owner may be restarting; custody remains here
                if receipt.wait(self._retry):
                    break
                with self._condition:
                    self._retries += 1

    def snapshot(self) -> EffectLaneSnapshot:
        with self._condition:
            return EffectLaneSnapshot(
                len(self._pending), self._rejected, self._retries, self._closed
            )

    def close(self, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)
