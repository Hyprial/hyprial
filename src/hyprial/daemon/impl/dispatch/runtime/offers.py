"""Per-worker bounded dispatch effects with actor-acknowledged outcomes."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest

from hyprial.daemon.impl.api  import HarnessDelivery
from hyprial.daemon.impl.inbox.contracts.api import InboxMessage


@dataclass(frozen=True, slots=True)
class DispatchOffer:
    operation_id: str
    generation: int
    worker: str
    delivery: HarnessDelivery
    original: InboxMessage
    retry: bool
    notice: bool


@dataclass(frozen=True, slots=True)
class DispatchOfferOutcome:
    operation_id: str
    generation: int
    worker: str
    delivery_id: str
    recipient: str
    original: InboxMessage
    accepted: bool
    retry: bool
    notice: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _TakeCompleted:
    operation_id: str


@dataclass(frozen=True, slots=True)
class _GetProjection:
    operation_id: str


@dataclass(frozen=True, slots=True)
class DispatchOfferProjection:
    pending: int
    completed: int
    overloaded: int
    failed: int
    closing: bool


@dataclass(slots=True)
class _Reply:
    ready: threading.Event = field(default_factory=threading.Event)
    value: object | None = None


class DispatchOfferCoordinator:
    """One active offer per worker; unrelated workers have parallel I/O lanes."""

    def __init__(
        self, dispatch, *, capacity: int = 32, workers: int = 4,
        runtime: ActorRuntime | None = None,
    ) -> None:
        self._dispatch = dispatch
        self._runtime = runtime or ActorRuntime()
        self._guard = threading.Lock()
        self._replies: dict[str, _Reply] = {}
        self._active: dict[str, DispatchOffer] = {}
        self._completed: dict[str, DispatchOfferOutcome] = {}
        self._capacity = capacity
        self._generation = 1
        self._overloaded = self._failed = 0
        self._closing = self._closed = False
        self._handle = self._runtime.start(
            ActorSpec(
                name="dispatch-offers",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity * 2,
            )
        )
        self._effects: EffectLane[DispatchOffer, DispatchOfferOutcome] = EffectLane(
            name="dispatch-offer-io",
            execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity,
            workers=workers,
        )

    def offer(
        self, worker: str, delivery: HarnessDelivery, *,
        original: InboxMessage, retry: bool, notice: bool,
    ) -> bool:
        command = DispatchOffer(
            uuid.uuid4().hex, self._generation, worker, delivery,
            original, retry, notice,
        )
        reply = _Reply()
        with self._guard:
            if self._closing or len(self._replies) >= self._capacity:
                self._overloaded += 1
                return False
            self._replies[command.operation_id] = reply
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                self._replies.pop(command.operation_id)
                self._overloaded += 1
                return False
        if not reply.ready.wait(1.0):
            # The accepted command still runs. A duplicate offer is fenced by
            # the actor's per-worker active slot when this command is handled.
            return False
        return reply.value is True

    def completed(self) -> tuple[DispatchOfferOutcome, ...]:
        command = _TakeCompleted(uuid.uuid4().hex)
        reply = _Reply()
        with self._guard:
            self._replies[command.operation_id] = reply
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                self._replies.pop(command.operation_id)
                return ()
        if not reply.ready.wait(1.0):
            return ()
        assert isinstance(reply.value, tuple)
        return reply.value

    def _execute(self, command: DispatchOffer) -> DispatchOfferOutcome:
        try:
            accepted = bool(self._dispatch(command.worker, command.delivery))
            error = None
        except Exception as caught:
            accepted = False
            error = f"{type(caught).__name__}: {caught}"[:500]
        return DispatchOfferOutcome(
            command.operation_id, command.generation, command.worker,
            command.delivery.delivery_id, command.delivery.recipient,
            command.original, accepted, command.retry, command.notice, error,
        )

    def _receive(self, command: object) -> None:
        if isinstance(command, DispatchOffer):
            if (
                command.worker in self._active
                or len(self._active) + len(self._completed) >= self._capacity
            ):
                with self._guard:
                    reply = self._replies.pop(command.operation_id, None)
                    if reply is not None:
                        reply.value = False
                        reply.ready.set()
                self._overloaded += 1
                return
            admitted = self._effects.submit(
                EffectRequest(command.operation_id, command.generation, command)
            )
            if admitted is AdmissionResult.ACCEPTED:
                self._active[command.worker] = command
            else:
                self._overloaded += 1
            with self._guard:
                reply = self._replies.pop(command.operation_id, None)
                if reply is not None:
                    reply.value = admitted is AdmissionResult.ACCEPTED
                    reply.ready.set()
            return
        if isinstance(command, EffectCompleted):
            result = command.result
            if isinstance(result, DispatchOfferOutcome):
                incumbent = self._active.get(result.worker)
                if incumbent is not None and incumbent.operation_id == result.operation_id:
                    self._active.pop(result.worker)
                    self._completed[result.operation_id] = result
                    self._failed += result.error is not None
            self._effects.acknowledge(command.operation_id, command.generation)
            return
        if isinstance(command, _TakeCompleted):
            outcomes = tuple(self._completed.values())
            self._completed.clear()
            with self._guard:
                reply = self._replies.pop(command.operation_id, None)
                if reply is not None:
                    reply.value = outcomes
                    reply.ready.set()
            return
        if isinstance(command, _GetProjection):
            snapshot = DispatchOfferProjection(
                len(self._active), len(self._completed),
                self._overloaded, self._failed, self._closing,
            )
            with self._guard:
                reply = self._replies.pop(command.operation_id, None)
                if reply is not None:
                    reply.value = snapshot
                    reply.ready.set()
            return
        raise TypeError("unsupported dispatch offer command")

    def projection(self) -> DispatchOfferProjection:
        command = _GetProjection(uuid.uuid4().hex)
        reply = _Reply()
        with self._guard:
            self._replies[command.operation_id] = reply
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                self._replies.pop(command.operation_id)
                raise TimeoutError("dispatch offer projection unavailable")
        if not reply.ready.wait(1.0):
            raise TimeoutError("dispatch offer projection remains pending")
        assert isinstance(reply.value, DispatchOfferProjection)
        return reply.value

    def drain(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self.projection().pending == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            if self._closed:
                return True
            self._closing = True
        if not self.drain(max(0.0, deadline - time.monotonic())):
            return False
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        stopped = self._runtime.stop(
            self._handle, max(0.0, deadline - time.monotonic())
        )
        with self._guard:
            self._closed = stopped
        return stopped
