"""Bounded inbox hold refresh effects with version-fenced state completion."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest

from hyprial.daemon.impl.dispatch.runtime.state  import DispatchStateAuthority


@dataclass(frozen=True, slots=True)
class RefreshHold:
    operation_id: str
    generation: int
    message_id: str
    expected: object | None
    replacement: object


@dataclass(frozen=True, slots=True)
class HoldRefreshProjection:
    pending: int
    rejected: int
    completed: int
    failed: int


class HoldRefreshCoordinator:
    """Keep native/SQLite hold writes off DispatchRuntime's tick effect."""

    def __init__(
        self, refresh, state: DispatchStateAuthority, *,
        capacity: int = 64, workers: int = 4,
        runtime: ActorRuntime | None = None,
    ) -> None:
        self._refresh = refresh
        self._state = state
        self._runtime = runtime or ActorRuntime()
        self._guard = threading.Lock()
        self._active: dict[str, RefreshHold] = {}
        self._rejected = self._completed = self._failed = 0
        self._closed = False
        self._capacity = capacity
        self._handle = self._runtime.start(
            ActorSpec(
                name="inbox-hold-refresh",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
            )
        )
        self._effects: EffectLane[RefreshHold, bool] = EffectLane(
            name="inbox-hold-refresh-io",
            execute=lambda request: bool(self._refresh(request.message_id)),
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity,
            workers=workers,
        )

    def submit(
        self, message_id: str, *, expected: object | None,
        replacement: object,
    ) -> AdmissionResult:
        command = RefreshHold(
            uuid.uuid4().hex, 1, message_id, expected, replacement
        )
        with self._guard:
            if self._closed:
                return AdmissionResult.CLOSED
            if message_id in self._active:
                return AdmissionResult.ACCEPTED
            if len(self._active) >= self._capacity:
                self._rejected += 1
                return AdmissionResult.OVERLOADED
            self._active[message_id] = command
            admitted = self._runtime.tell(self._handle, command)
            if admitted is not AdmissionResult.ACCEPTED:
                self._active.pop(message_id)
                self._rejected += 1
            return admitted

    def _receive(self, command: object) -> None:
        if isinstance(command, RefreshHold):
            admitted = self._effects.submit(
                EffectRequest(command.operation_id, command.generation, command)
            )
            if admitted is not AdmissionResult.ACCEPTED:
                with self._guard:
                    self._active.pop(command.message_id, None)
                    self._rejected += 1
            return
        if isinstance(command, EffectCompleted):
            with self._guard:
                request = next(
                    (
                        item for item in self._active.values()
                        if item.operation_id == command.operation_id
                    ),
                    None,
                )
            if request is None:
                self._effects.acknowledge(command.operation_id, command.generation)
                return
            # Complete the state transition before acknowledging the effect.
            # A late refresh cannot recreate a hold removed by settlement or
            # overwrite the next attempt's newer timestamp.
            if command.error or not command.result:
                self._state.settle_hold(
                    request.message_id, expected=request.expected,
                    replacement=None,
                )
                failed = True
            else:
                self._state.settle_hold(
                    request.message_id, expected=request.expected,
                    replacement=request.replacement,
                )
                failed = False
            with self._guard:
                self._active.pop(request.message_id, None)
                self._completed += 1
                self._failed += failed
            self._effects.acknowledge(command.operation_id, command.generation)
            return
        raise TypeError("unsupported hold refresh command")

    def projection(self) -> HoldRefreshProjection:
        with self._guard:
            return HoldRefreshProjection(
                len(self._active), self._rejected, self._completed, self._failed
            )

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closed = True
        while True:
            with self._guard:
                pending = bool(self._active)
            if not pending:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        return self._runtime.stop(
            self._handle, max(0.0, deadline - time.monotonic())
        )
