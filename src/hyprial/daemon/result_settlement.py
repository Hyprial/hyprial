"""Bounded, per-delivery terminal settlement with retained Harness claims."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.actor_runtime.effects import EffectCompleted, EffectLane, EffectRequest

from .harness_ports import ClaimedHarnessResult


@dataclass(frozen=True, slots=True)
class SettleClaim:
    operation_id: str
    generation: int
    claim: ClaimedHarnessResult


@dataclass(frozen=True, slots=True)
class SettleOutcome:
    operation_id: str
    generation: int
    delivery_id: str
    claim_token: str
    settled: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SettlementProjection:
    pending: int
    completed: int
    failed: int
    overloaded: int
    closing: bool


class ResultSettlementCoordinator:
    """One effect per delivery; unrelated deliveries settle concurrently.

    Harness retains the frozen claim until ``settle_result`` confirms it.
    Timeouts, effect errors and owner restart leave that claim replayable.
    """

    def __init__(
        self,
        settle: Callable[[ClaimedHarnessResult], bool],
        *,
        capacity: int = 64,
        workers: int = 4,
        runtime: ActorRuntime | None = None,
    ) -> None:
        if capacity < 1 or workers < 1:
            raise ValueError("settlement capacity and workers must be positive")
        self._settle = settle
        self._capacity = capacity
        self._runtime = runtime or ActorRuntime()
        self._guard = threading.Lock()
        self._active: dict[str, SettleClaim] = {}
        self._generation = 1
        self._completed = self._failed = self._overloaded = 0
        self._closing = self._closed = False
        self._handle = self._runtime.start(ActorSpec(
            name="harness-result-settlement",
            handler_factory=lambda: self._receive,
            mailbox_capacity=capacity,
        ))
        self._effects: EffectLane[SettleClaim, SettleOutcome] = EffectLane(
            name="harness-result-settlement-io",
            execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity,
            workers=workers,
        )

    def submit(self, claim: ClaimedHarnessResult) -> AdmissionResult:
        with self._guard:
            if self._closing:
                return AdmissionResult.CLOSED
            # Harness claims are replayed on every scan. The same token is
            # already owned; a new token for one delivery waits for settlement.
            if claim.result.delivery_id in self._active:
                return AdmissionResult.ACCEPTED
            if len(self._active) >= self._capacity:
                self._overloaded += 1
                return AdmissionResult.OVERLOADED
            command = SettleClaim(uuid.uuid4().hex, self._generation, claim)
            self._active[claim.result.delivery_id] = command
            admitted = self._runtime.tell(self._handle, command)
            if admitted is not AdmissionResult.ACCEPTED:
                self._active.pop(claim.result.delivery_id, None)
                self._overloaded += 1
            return admitted

    def projection(self) -> SettlementProjection:
        with self._guard:
            return SettlementProjection(
                len(self._active), self._completed, self._failed,
                self._overloaded, self._closing,
            )

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            if self._closed:
                return True
            self._closing = True
        while self.projection().pending:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        stopped = self._runtime.stop(
            self._handle, max(0.0, deadline - time.monotonic())
        )
        with self._guard:
            self._closed = stopped
        return stopped

    def _execute(self, command: SettleClaim) -> SettleOutcome:
        try:
            settled = bool(self._settle(command.claim))
            error = None
        except Exception as caught:
            settled = False
            error = f"{type(caught).__name__}: {caught}"[:500]
        return SettleOutcome(
            command.operation_id, command.generation,
            command.claim.result.delivery_id, command.claim.claim_token,
            settled, error,
        )

    def _receive(self, command: object) -> None:
        if isinstance(command, SettleClaim):
            admitted = self._effects.submit(EffectRequest(
                command.operation_id, command.generation, command,
            ))
            if admitted is not AdmissionResult.ACCEPTED:
                with self._guard:
                    incumbent = self._active.get(command.claim.result.delivery_id)
                    if incumbent == command:
                        self._active.pop(command.claim.result.delivery_id)
                    self._overloaded += 1
            return
        if isinstance(command, EffectCompleted):
            outcome = command.result
            if isinstance(outcome, SettleOutcome):
                with self._guard:
                    incumbent = self._active.get(outcome.delivery_id)
                    if (
                        incumbent is not None
                        and incumbent.operation_id == outcome.operation_id
                        and incumbent.generation == outcome.generation
                    ):
                        self._active.pop(outcome.delivery_id)
                        self._completed += 1
                        self._failed += bool(outcome.error)
            self._effects.acknowledge(command.operation_id, command.generation)
            return
        raise TypeError(f"unsupported settlement command {type(command).__name__}")
