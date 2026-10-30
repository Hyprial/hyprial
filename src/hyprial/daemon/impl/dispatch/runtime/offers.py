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


class DispatchAgentTimeout(RuntimeError):
    """One worker's bounded dispatch offer did not settle in its budget."""

    def __init__(self, operation_id: str, elapsed_ms: int) -> None:
        super().__init__(f"dispatch offer {operation_id} timed out after {elapsed_ms}ms")
        self.operation_id = operation_id
        self.elapsed_ms = elapsed_ms


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
    message_ids: tuple[str, ...] = ()


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


@dataclass(frozen=True, slots=True)
class _EffectLanesSnapshot:
    outstanding: int
    rejected: int
    completion_retries: int
    closed: bool


class _EffectLanes:
    """One effect lane per worker, so a hung worker cannot occupy a peer lane."""

    def __init__(
        self,
        *,
        execute,
        complete,
        capacity: int,
        workers: int,
    ) -> None:
        self._execute = execute
        self._complete = complete
        self._capacity = capacity
        self._workers = max(1, workers)
        self._guard = threading.Lock()
        self._lanes: dict[str, EffectLane[DispatchOffer, DispatchOfferOutcome]] = {}
        self._closed = False

    def _lane(self, worker: str) -> EffectLane[DispatchOffer, DispatchOfferOutcome]:
        with self._guard:
            lane = self._lanes.get(worker)
            if lane is None:
                lane = EffectLane(
                    name=f"dispatch-offer-io-{len(self._lanes)}",
                    execute=self._execute,
                    complete=self._complete,
                    capacity=max(1, self._capacity),
                    workers=self._workers,
                )
                self._lanes[worker] = lane
            return lane

    def submit(self, worker: str, request: EffectRequest[DispatchOffer]) -> AdmissionResult:
        return self._lane(worker).submit(request)

    def acknowledge(
        self, worker: str, operation_id: str, generation: int
    ) -> bool:
        with self._guard:
            lane = self._lanes.get(worker)
        if lane is None:
            return False
        acknowledged = lane.acknowledge(operation_id, generation)
        if acknowledged and lane.snapshot().outstanding == 0:
            lane.close(0.05)
            with self._guard:
                if self._lanes.get(worker) is lane:
                    self._lanes.pop(worker, None)
        return acknowledged

    def snapshot(self) -> _EffectLanesSnapshot:
        with self._guard:
            lanes = tuple(self._lanes.values())
        snapshots = tuple(lane.snapshot() for lane in lanes)
        return _EffectLanesSnapshot(
            sum(item.outstanding for item in snapshots),
            sum(item.rejected for item in snapshots),
            sum(item.completion_retries for item in snapshots),
            bool(snapshots) and all(item.closed for item in snapshots),
        )

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closed = True
            lanes = tuple(self._lanes.values())
        results = [
            lane.close(max(0.0, deadline - time.monotonic())) for lane in lanes
        ]
        results.extend(lane.close(0.0) for lane in lanes)
        return all(results)


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
        self._active_since: dict[str, float] = {}
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
        self._effects = _EffectLanes(
            execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity,
            workers=workers,
        )

    def offer(
        self, worker: str, delivery: HarnessDelivery, *,
        original: InboxMessage, retry: bool, notice: bool,
    ) -> bool:
        return self._offer(worker, delivery, original=original, retry=retry, notice=notice)

    def offer_sync(
        self, worker: str, delivery: HarnessDelivery, *,
        original: InboxMessage, retry: bool, notice: bool, timeout_ms: int,
    ) -> bool:
        timeout = max(0.001, timeout_ms / 1000)
        deadline = time.monotonic() + timeout
        command = self._submit(
            worker, delivery, original=original, retry=retry, notice=notice,
            wait_seconds=timeout,
        )
        if command is None:
            elapsed_ms = max(0, int((time.monotonic() - deadline + timeout) * 1000))
            if elapsed_ms >= timeout_ms:
                raise DispatchAgentTimeout("dispatch-offer-admission", elapsed_ms)
            return False
        remaining = max(0.0, deadline - time.monotonic())
        if not command[1].ready.wait(remaining):
            raise DispatchAgentTimeout(command[0].operation_id, timeout_ms)
        if command[1].value is not True:
            return False
        operation_id = command[0].operation_id
        while True:
            outcome = self._completed.pop(operation_id, None)
            if outcome is not None:
                return outcome.accepted
            if time.monotonic() >= deadline:
                elapsed_ms = max(timeout_ms, int(timeout * 1000))
                raise DispatchAgentTimeout(operation_id, elapsed_ms)
            time.sleep(0.005)

    def _offer(
        self, worker: str, delivery: HarnessDelivery, *,
        original: InboxMessage, retry: bool, notice: bool,
    ) -> bool:
        command = self._submit(
            worker, delivery, original=original, retry=retry, notice=notice
        )
        if command is None:
            return False
        return command[1].value is True

    def _submit(
        self, worker: str, delivery: HarnessDelivery, *,
        original: InboxMessage, retry: bool, notice: bool,
        wait_seconds: float = 1.0,
    ) -> tuple[DispatchOffer, _Reply] | None:
        command = DispatchOffer(
            uuid.uuid4().hex, self._generation, worker, delivery,
            original, retry, notice,
        )
        reply = _Reply()
        with self._guard:
            if self._closing or len(self._replies) >= self._capacity:
                self._overloaded += 1
                return None
            self._replies[command.operation_id] = reply
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                self._replies.pop(command.operation_id)
                self._overloaded += 1
                return None
        if not reply.ready.wait(wait_seconds):
            # The accepted command still runs. A duplicate offer is fenced by
            # the actor's per-worker active slot when this command is handled.
            return None
        return command, reply

    def active(self, worker: str) -> DispatchOffer | None:
        return self._active.get(worker)

    def active_elapsed_ms(self, worker: str, *, now: float | None = None) -> int:
        started = self._active_since.get(worker)
        if started is None:
            return 0
        return max(0, int(((now if now is not None else time.monotonic()) - started) * 1000))

    def active_operation_ids(self) -> frozenset[str]:
        return frozenset(command.operation_id for command in self._active.values())

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
            tuple(command.delivery.message_ids),
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
                command.worker,
                EffectRequest(command.operation_id, command.generation, command),
            )
            if admitted is AdmissionResult.ACCEPTED:
                self._active[command.worker] = command
                self._active_since[command.worker] = time.monotonic()
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
            outcome = result if isinstance(result, DispatchOfferOutcome) else None
            worker = outcome.worker if outcome is not None else None
            settled: DispatchOfferOutcome | None = None
            if worker is None:
                for candidate in self._active.values():
                    if (
                        candidate.operation_id == command.operation_id
                        and candidate.generation == command.generation
                    ):
                        worker = candidate.worker
                        settled = DispatchOfferOutcome(
                            candidate.operation_id,
                            candidate.generation,
                            candidate.worker,
                            candidate.delivery.delivery_id,
                            candidate.delivery.recipient,
                            candidate.original,
                            False,
                            candidate.retry,
                            candidate.notice,
                            command.error or "effect raised BaseException",
                            tuple(candidate.delivery.message_ids),
                        )
                        self._active.pop(worker, None)
                        self._active_since.pop(worker, None)
                        self._failed += 1
                        break
            if worker is None:
                return
            if outcome is not None:
                incumbent = self._active.get(worker)
                if incumbent is not None and incumbent.operation_id == outcome.operation_id:
                    self._active.pop(worker)
                    self._active_since.pop(worker, None)
                    settled = outcome
                    self._failed += outcome.error is not None
            self._effects.acknowledge(worker, command.operation_id, command.generation)
            if settled is not None:
                self._completed[settled.operation_id] = settled
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
