"""Actor-owned turn admission, FIFO ownership, and stable projections.

The concrete harness clients remain I/O adapters.  This coordinator is the
single mutable owner of delivery ordering and terminal result publication; it
uses only the public :mod:`hyprial.actor_runtime` surface and the frozen typed turn
ports.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
import logging

from hyprial.actor_runtime import (
    PROCESS_LIFECYCLE,
    ActorEvent,
    ActorEventKind,
    ActorRuntime,
    ActorSpec,
    AdmissionResult,
    ExpectedActorError,
)
from hyprial.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.daemon.api import classify_harness_failure

from .turn_ports import (
    CloseTurnPumpCommand,
    EnqueueTurnCommand,
    InterruptTurnCommand,
    TurnCommand,
    TurnDeliveryProjection,
    TurnEvent,
    TurnInterruptIoCompleted,
    TurnIoCompleted,
    TurnProgressObserved,
    TurnPumpClosed,
    TurnResultProjection,
    TurnStarted,
)


TurnIoSubmit = Callable[[int, int, TurnDeliveryProjection], None]
TurnIoInterrupt = Callable[[int, int, str, str], None]
TurnIoClose = Callable[[int, int, str], None]
TurnIoAbort = Callable[[int, Callable[[], None]], None]
TurnIoFailureDecision = Callable[[int, int, str, bool, int, str], None]
TurnEventSink = Callable[[TurnEvent], None]

_LOGGER = logging.getLogger("hyprial.turn_runtime")

#: Enumerable markers for model-vendor failures that cannot heal on a
#: later attempt, so burning the attempt budget on them is pure cost
#: (card 260: an expired OAuth grant was retried by count alone).  The
#: check is plain case-insensitive substring containment against this
#: closed list -- deliberately NOT a regex over free-form error text.
#
#: Authentication/authorization ONLY, on purpose.  pi 0.84.4 keeps two
#: closed error tables of its own (pi-ai ``utils/retry.js``:
#: ``RETRYABLE_PROVIDER_ERROR_PATTERN`` and
#: ``NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN``), and its retryable
#: table contains network wording verbatim -- five of the eight network
#: markers this list once carried ("fetch failed", "getaddrinfo",
#: "ENOTFOUND", "EAI_AGAIN", "socket hang up") appear there word for
#: word, and "ECONNREFUSED"/"ECONNRESET" are the same transient class.
#: Keeping them here would have pi retry what hyprial kills at attempt 1:
#: DNS jitter, a vendor restart, or a laptop waking up would all die on
#: the first failure.  The string alone cannot tell a transient network
#: blip from a dead credential behind it -- the crh "fetch failed" was
#: terminal-worthy only because its cause was an expired OAuth grant,
#: information the string does not carry -- so network-class failures
#: stay retryable here and the attempt count is the backstop.
#:
#: Deliberately NOT pi's classifier either: this list shares a class
#: boundary with pi's tables (auth text is in neither of them, so pi
#: fails fast on it too) but copies no decision from pi internals.
#: Retrying a wrong/expired credential can never succeed; that is the
#: one class where "terminal on first attempt" is safe from the text
#: alone.
_TERMINAL_PROVIDER_ERROR_MARKERS: frozenset[str] = frozenset(
    {
        # authentication / authorization
        "unauthorized",
        "authentication failed",
        "failed to authenticate",
        "invalid api key",
        "invalid_api_key",
        "oauth",
        "token refresh",
        "expired token",
        "permission denied",
    }
)


def provider_failure_is_terminal(error: str) -> bool:
    """True when a failure text names a model-vendor auth cause.

    Matched against the closed auth marker list above by case-insensitive
    substring containment; network-class wording is deliberately absent
    (see the list's comment).  The verbatim providerError text reaches
    here via the connector's failure strings (turn outcome errors and
    process-exit stderr tails); any error outside the list keeps the
    configured attempt budget unchanged.
    """

    detail = (error or "").casefold()
    return any(marker in detail for marker in _TERMINAL_PROVIDER_ERROR_MARKERS)


def _log_turn_event(event: TurnEvent) -> None:
    _LOGGER.info(
        "turn runtime event",
        extra={
            "turn_event": {
                "kind": type(event).__name__,
                "correlationId": event.correlation_id,
                "generation": event.generation,
                "version": event.version,
            }
        },
    )


@dataclass(frozen=True, slots=True)
class _IoCompleted:
    generation: int
    version: int
    result: TurnResultProjection


@dataclass(frozen=True, slots=True)
class _InterruptCompleted:
    generation: int
    version: int
    correlation_id: str
    delivery_id: str
    interrupted: bool
    detail: str | None


@dataclass(frozen=True, slots=True)
class _RecoveryReady:
    generation: int
    failed_delivery_id: str | None


@dataclass(frozen=True, slots=True)
class _IoFailed:
    generation: int
    version: int
    delivery_id: str
    error: str
    allow_retry: bool = True


_InternalCompletion = _IoCompleted | _InterruptCompleted | _RecoveryReady | _IoFailed


class _TurnProjection:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.in_flight: TurnDeliveryProjection | None = None
        self.results: deque[TurnResultProjection] = deque()


class _AdmissionGate:
    """Bound work retained beyond the backend mailbox's short admission."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._known: set[str] = set()
        self._commands: dict[str, EnqueueTurnCommand] = {}
        self._closing = False
        self._lock = threading.Lock()

    def reserve(self, delivery_id: str) -> tuple[PortAdmission, str]:
        """Admit a delivery, reporting which rule decided it.

        Two unrelated conditions both yield OVERLOADED and they call for
        opposite responses: an id already held is waiting on the daemon to
        drain its result (or on this process being rebuilt), while a full gate
        is capacity pressure.  One value for both left the caller unable to
        say which -- the same shape as a turn failure logged only as "failed",
        where the reason was in hand and never spoken.

        The reason rides alongside rather than replacing the admission, so
        this is a private widening: the public submit() signature, and every
        caller of it, is unchanged.
        """

        with self._lock:
            if self._closing:
                return PortAdmission.CLOSING, "closing"
            if delivery_id in self._known:
                return PortAdmission.OVERLOADED, "already-reserved"
            if len(self._known) >= self._capacity:
                return PortAdmission.OVERLOADED, "capacity"
            self._known.add(delivery_id)
            return PortAdmission.ACCEPTED, "accepted"

    def remember(self, command: EnqueueTurnCommand) -> None:
        with self._lock:
            self._commands[command.delivery.delivery_id] = command

    def release(self, delivery_id: str) -> None:
        with self._lock:
            self._known.discard(delivery_id)
            self._commands.pop(delivery_id, None)

    def close(self) -> None:
        with self._lock:
            self._closing = True

    def commands(self) -> tuple[EnqueueTurnCommand, ...]:
        with self._lock:
            return tuple(self._commands.values())


class _CompletionRelay:
    """One bounded retry lane so terminal completions cannot be dropped."""

    def __init__(self, deliver: Callable[[_InternalCompletion], AdmissionResult]) -> None:
        import queue

        self._queue: queue.Queue[_InternalCompletion | None] = queue.Queue(maxsize=8)
        self._deliver = deliver
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="hyprial-turn-completion-relay",
            daemon=True,
        )
        self._thread.start()

    def submit(
        self, completion: _InternalCompletion, *, timeout: float = 0.25
    ) -> PortAdmission:
        import queue

        deadline = time.monotonic() + max(0.0, timeout)
        while not self._closed.is_set():
            try:
                self._queue.put(
                    completion,
                    timeout=min(0.05, max(0.0, deadline - time.monotonic())),
                )
            except queue.Full:
                if time.monotonic() >= deadline:
                    return PortAdmission.OVERLOADED
                continue
            return PortAdmission.ACCEPTED
        return PortAdmission.CLOSING

    def close(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.002)
        if self._queue.unfinished_tasks:
            self._closed.set()
            self._thread.join(max(0.0, deadline - time.monotonic()))
            return False
        self._closed.set()
        self._queue.put_nowait(None)
        self._thread.join(max(0.0, deadline - time.monotonic()))
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            completion = self._queue.get()
            try:
                if completion is None:
                    return
                while not self._closed.is_set():
                    result = self._deliver(completion)
                    if result is AdmissionResult.ACCEPTED:
                        break
                    if result is AdmissionResult.CLOSED:
                        break
                    time.sleep(0.002)
            finally:
                self._queue.task_done()


class _TurnShard:
    def __init__(
        self,
        *,
        generation: int,
        projection: _TurnProjection,
        event_sink: TurnEventSink,
        io_submit: TurnIoSubmit,
        io_interrupt: TurnIoInterrupt,
        io_close: TurnIoClose,
        io_failure_decision: TurnIoFailureDecision,
        max_delivery_attempts: int,
        recovery_commands: tuple[EnqueueTurnCommand, ...] = (),
    ) -> None:
        self._generation = generation
        self._projection = projection
        self._event_sink = event_sink
        self._io_submit = io_submit
        self._io_interrupt = io_interrupt
        self._io_close = io_close
        self._io_failure_decision = io_failure_decision
        self._max_delivery_attempts = max_delivery_attempts
        self._pending: deque[EnqueueTurnCommand] = deque()
        self._current: EnqueueTurnCommand | None = None
        self._interrupted: set[str] = set()
        self._version = 0
        self._closing = False
        self._attempts: dict[str, int] = {}
        self._recovery_commands = recovery_commands
        self._recovering = bool(recovery_commands)

    def __call__(self, command: object) -> None:
        if isinstance(command, EnqueueTurnCommand):
            self._enqueue(command)
        elif isinstance(command, InterruptTurnCommand):
            self._interrupt(command)
        elif isinstance(command, CloseTurnPumpCommand):
            self._close(command)
        elif isinstance(command, _IoCompleted):
            self._complete(command)
        elif isinstance(command, _InterruptCompleted):
            self._interrupt_completed(command)
        elif isinstance(command, _RecoveryReady):
            self._recovery_ready(command)
        elif isinstance(command, _IoFailed):
            self._io_failed(command)
        else:
            raise ExpectedActorError(
                "TURN_COMMAND_UNSUPPORTED",
                f"unsupported turn command: {type(command).__name__}",
            )

    def _enqueue(self, command: EnqueueTurnCommand) -> None:
        if self._closing:
            self._reject(command.correlation_id, "TURN_RUNTIME_CLOSING")
            return
        if self._recovering:
            self._pending.append(command)
        elif self._current is None:
            self._start(command)
        else:
            self._pending.append(command)

    def _start(self, command: EnqueueTurnCommand) -> None:
        self._current = command
        self._version += 1
        with self._projection.lock:
            self._projection.in_flight = command.delivery
        self._event_sink(
            TurnStarted(
                correlation_id=command.correlation_id,
                generation=self._generation,
                version=self._version,
                delivery_id=command.delivery.delivery_id,
            )
        )
        # This callback is an in-memory handoff to the bounded I/O worker.  It
        # must never perform app-server, process, or network I/O itself.
        self._io_submit(self._generation, self._version, command.delivery)

    def _complete(self, command: _IoCompleted) -> None:
        current = self._current
        if (
            command.generation != self._generation
            or current is None
            or command.result.delivery_id != current.delivery.delivery_id
        ):
            return
        result = command.result
        if result.delivery_id in self._interrupted:
            result = replace(
                result,
                status="interrupted",
                output="",
                error="harness turn interrupted",
                failure_code=None,
            )
            self._interrupted.discard(result.delivery_id)
        self._version += 1
        with self._projection.lock:
            self._projection.results.append(result)
            self._projection.in_flight = None
        self._event_sink(
            TurnIoCompleted(
                correlation_id=current.correlation_id,
                generation=self._generation,
                version=self._version,
                result=result,
            )
        )
        self._current = None
        self._attempts.pop(result.delivery_id, None)
        if self._pending and not self._closing:
            self._start(self._pending.popleft())

    def _interrupt(self, command: InterruptTurnCommand) -> None:
        current = self._current
        if current is None or current.delivery.delivery_id != command.delivery_id:
            self._reject(command.correlation_id, "TURN_NOT_IN_FLIGHT")
            return
        self._version += 1
        self._interrupted.add(command.delivery_id)
        self._io_interrupt(
            self._generation,
            self._version,
            command.correlation_id,
            command.delivery_id,
        )

    def _interrupt_completed(self, command: _InterruptCompleted) -> None:
        if command.generation != self._generation:
            return
        self._version += 1
        self._event_sink(
            TurnInterruptIoCompleted(
                correlation_id=command.correlation_id,
                generation=self._generation,
                version=self._version,
                delivery_id=command.delivery_id,
                interrupted=command.interrupted,
                detail=command.detail,
            )
        )

    def _io_failed(self, command: _IoFailed) -> None:
        current = self._current
        if (
            command.generation != self._generation
            or current is None
            or command.delivery_id != current.delivery.delivery_id
        ):
            return
        attempt = self._attempts.get(command.delivery_id, 0) + 1
        self._attempts[command.delivery_id] = attempt
        self._version += 1
        # Category joins count: a wrong/expired credential cannot heal by
        # re-prompting, so it is terminal on the FIRST attempt no matter
        # how much budget remains; everything else -- including all
        # network-class wording -- retries by count exactly as before
        # (card 260, review round 2 B1).
        terminal = provider_failure_is_terminal(command.error)
        retry = command.allow_retry and attempt < self._max_delivery_attempts and not terminal
        self._event_sink(
            TurnProgressObserved(
                correlation_id=current.correlation_id,
                generation=self._generation,
                version=self._version,
                delivery_id=command.delivery_id,
                sequence=attempt,
                phase="turn-retry" if retry else "turn-abandoned",
                summary=command.error,
            )
        )
        if not retry:
            result = TurnResultProjection(
                delivery_id=command.delivery_id,
                recipient=current.delivery.recipient,
                status="failed",
                error=(
                    f"delivery abandoned after {attempt} turn attempts; "
                    f"last failure: {command.error}"
                ),
                # A credential failure stopped this turn on purpose; hand the
                # daemon that verdict.  A fixed transient code here made the
                # daemon redeliver the turn up to its own attempt limit, so a
                # rejected key still re-ran every side effect several times.
                failure_code=(
                    classify_harness_failure(command.error)
                    if terminal
                    else "HARNESS_TRANSIENT_FAILURE"
                ),
            )
            with self._projection.lock:
                self._projection.results.append(result)
                self._projection.in_flight = None
            self._event_sink(
                TurnIoCompleted(
                    correlation_id=current.correlation_id,
                    generation=self._generation,
                    version=self._version,
                    result=result,
                )
            )
            self._current = None
            self._attempts.pop(command.delivery_id, None)
        self._io_failure_decision(
            self._generation,
            self._version,
            command.delivery_id,
            retry,
            attempt,
            command.error,
        )
        if not retry and self._pending and not self._closing:
            self._start(self._pending.popleft())

    def _close(self, command: CloseTurnPumpCommand) -> None:
        if self._closing:
            return
        self._closing = True
        if self._current is not None:
            self._interrupted.add(self._current.delivery.delivery_id)
        self._version += 1
        self._io_close(self._generation, self._version, command.correlation_id)
        self._event_sink(
            TurnPumpClosed(
                correlation_id=command.correlation_id,
                generation=self._generation,
                version=self._version,
                remaining_delivery_id=(
                    self._current.delivery.delivery_id
                    if self._current is not None
                    else None
                ),
            )
        )

    def _recovery_ready(self, command: _RecoveryReady) -> None:
        if command.generation not in {0, self._generation} or not self._recovering:
            return
        self._recovering = False
        recovered = self._recovery_commands
        self._recovery_commands = ()
        admitted_during_recovery = tuple(self._pending)
        self._pending.clear()
        for queued in recovered:
            if queued.delivery.delivery_id == command.failed_delivery_id:
                self._version += 1
                result = TurnResultProjection(
                    delivery_id=queued.delivery.delivery_id,
                    recipient=queued.delivery.recipient,
                    status="failed",
                    error="turn actor failed while native I/O was in flight",
                    failure_code="HARNESS_TRANSIENT_FAILURE",
                )
                with self._projection.lock:
                    self._projection.results.append(result)
                self._event_sink(
                    TurnIoCompleted(
                        correlation_id=queued.correlation_id,
                        generation=self._generation,
                        version=self._version,
                        result=result,
                    )
                )
            else:
                self._pending.append(queued)
        self._pending.extend(admitted_during_recovery)
        if self._pending and not self._closing:
            self._start(self._pending.popleft())

    def _reject(self, correlation_id: str, code: str) -> None:
        self._event_sink(
            PortCommandRejected(
                correlation_id=correlation_id,
                domain="turn",
                generation=self._generation,
                version=self._version,
                code=code,
                detail=code.replace("_", " ").lower(),
            )
        )


class TurnRuntime:
    """One fixed turn shard with bounded admission and typed projections."""

    def __init__(
        self,
        *,
        name: str,
        io_submit: TurnIoSubmit,
        io_interrupt: TurnIoInterrupt,
        io_close: TurnIoClose,
        io_abort: TurnIoAbort,
        io_failure_decision: TurnIoFailureDecision,
        max_delivery_attempts: int = 5,
        event_sink: TurnEventSink | None = None,
        capacity: int = 128,
    ) -> None:
        if capacity < 1:
            raise ValueError("turn runtime capacity must be at least 1")
        if max_delivery_attempts < 1:
            raise ValueError("a delivery needs at least one attempt")
        self._projection = _TurnProjection()
        self._admission = _AdmissionGate(capacity)
        self._events = event_sink or _log_turn_event
        self._io_abort = io_abort
        self._generation = 0
        self._recovery_lock = threading.RLock()
        self._recovery_ready: tuple[int, str | None] | None = None
        self._runtime = ActorRuntime(event_sink=self._on_actor_event)

        def factory() -> _TurnShard:
            self._generation += 1
            with self._projection.lock:
                self._projection.in_flight = None
            return _TurnShard(
                generation=self._generation,
                projection=self._projection,
                event_sink=self._events,
                io_submit=io_submit,
                io_interrupt=io_interrupt,
                io_close=io_close,
                io_failure_decision=io_failure_decision,
                max_delivery_attempts=max_delivery_attempts,
                recovery_commands=(
                    self._admission.commands() if self._generation > 1 else ()
                ),
            )

        self._handle = self._runtime.start(
            ActorSpec(
                name=f"turn:{name}",
                handler_factory=factory,
                mailbox_capacity=capacity,
                supervision_profile=PROCESS_LIFECYCLE,
            )
        )
        self._relay = _CompletionRelay(
            lambda completion: self._runtime.tell(self._handle, completion)
        )

    def submit(self, command: TurnCommand) -> PortAdmission:
        reserved = False
        if isinstance(command, EnqueueTurnCommand):
            admission, reason = self._admission.reserve(
                command.delivery.delivery_id
            )
            if admission is not PortAdmission.ACCEPTED:
                # Said out loud because the caller only sees a bool: a
                # delivery refused as already-reserved is waiting on the
                # daemon to drain it, and one refused for capacity is not.
                # Without this the holder cannot tell either from an
                # unhealthy process.
                _LOGGER.info(
                    "turn runtime admission refused",
                    extra={
                        "turn_admission": {
                            "deliveryId": command.delivery.delivery_id,
                            "admission": admission.value,
                            "reason": reason,
                        }
                    },
                )
                return admission
            self._admission.remember(command)
            reserved = True
        elif isinstance(command, CloseTurnPumpCommand):
            self._admission.close()
        result = self._runtime.tell(self._handle, command)
        mapped = self._map_admission(result)
        if reserved and mapped is not PortAdmission.ACCEPTED:
            self._admission.release(command.delivery.delivery_id)
        return mapped

    def complete_io(
        self,
        *,
        generation: int,
        version: int,
        result: TurnResultProjection,
    ) -> PortAdmission:
        return self._relay.submit(
            _IoCompleted(
                generation=generation,
                version=version,
                result=result,
            )
        )

    def complete_interrupt(
        self,
        *,
        generation: int,
        version: int,
        correlation_id: str,
        delivery_id: str,
        interrupted: bool,
        detail: str | None = None,
    ) -> PortAdmission:
        return self._relay.submit(
            _InterruptCompleted(
                generation=generation,
                version=version,
                correlation_id=correlation_id,
                delivery_id=delivery_id,
                interrupted=interrupted,
                detail=detail,
            )
        )

    def fail_io(
        self,
        *,
        generation: int,
        version: int,
        delivery_id: str,
        error: str,
        allow_retry: bool = True,
    ) -> PortAdmission:
        return self._relay.submit(
            _IoFailed(
                generation=generation,
                version=version,
                delivery_id=delivery_id,
                error=error,
                allow_retry=allow_retry,
            )
        )

    def read_in_flight(self) -> TurnDeliveryProjection | None:
        with self._projection.lock:
            return self._projection.in_flight

    def read_results(self) -> tuple[TurnResultProjection, ...]:
        with self._projection.lock:
            return tuple(self._projection.results)

    def drain_results(self) -> tuple[TurnResultProjection, ...]:
        with self._projection.lock:
            results = tuple(self._projection.results)
            self._projection.results.clear()
        for result in results:
            self._admission.release(result.delivery_id)
        return results

    def drain(self, timeout: float) -> bool:
        started = time.monotonic()
        if not self._relay.close(timeout):
            return False
        remaining = max(0.0, timeout - (time.monotonic() - started))
        return self._runtime.drain(remaining).complete

    def _on_actor_event(self, event: ActorEvent) -> None:
        _LOGGER.info(
            "turn actor lifecycle",
            extra={
                "turn_actor_event": {
                    "kind": event.kind.value,
                    "actorId": event.handle.actor_id,
                    "generation": event.generation,
                    "code": event.code,
                }
            },
        )
        if event.kind is ActorEventKind.CHILD_FAILED:
            with self._projection.lock:
                in_flight = self._projection.in_flight
                self._projection.in_flight = None
            failed_delivery_id = (
                in_flight.delivery_id if in_flight is not None else None
            )
            failed_generation = event.generation

            def ready(
                generation: int = failed_generation,
                delivery_id: str | None = failed_delivery_id,
            ) -> None:
                with self._recovery_lock:
                    self._recovery_ready = (generation, delivery_id)
                self._release_recovery_if_ready()

            self._io_abort(failed_generation, ready)
        elif event.kind is ActorEventKind.CHILD_RESTARTED:
            self._release_recovery_if_ready()

    def _release_recovery_if_ready(self) -> None:
        with self._recovery_lock:
            ready = self._recovery_ready
            if ready is None or self._generation <= ready[0]:
                return
            self._recovery_ready = None
        admission = self._relay.submit(
            _RecoveryReady(
                generation=self._generation,
                failed_delivery_id=ready[1],
            ),
            timeout=0.25,
        )
        if admission is not PortAdmission.ACCEPTED:
            with self._recovery_lock:
                self._recovery_ready = ready

    @staticmethod
    def _map_admission(result: AdmissionResult) -> PortAdmission:
        if result is AdmissionResult.ACCEPTED:
            return PortAdmission.ACCEPTED
        if result is AdmissionResult.OVERLOADED:
            return PortAdmission.OVERLOADED
        return PortAdmission.CLOSING


__all__ = [
    "TurnRuntime",
    "TurnDeliveryProjection",
    "TurnProgressObserved",
    "TurnResultProjection",
]
