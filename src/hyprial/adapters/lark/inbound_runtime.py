"""Actor admission and ordering for Lark inbound SDK callbacks.

SDK websocket threads hand immutable messages to this boundary.  The actor owns
in-flight event/message deduplication and correlation ordering; the blocking
SQLite, daemon IPC, and Lark REST work executes on a bounded effect pool and
returns a generation-fenced completion.  No actor handler performs I/O.
"""

from __future__ import annotations

import threading
import time
import uuid
import weakref
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Callable

from hyprial.actor_runtime import ActorHandle, ActorRuntime, ActorSpec, AdmissionResult

from .api import InboundOutcome, LarkInboundMessage


@dataclass(frozen=True, slots=True)
class _SubmitInbound:
    correlation_id: str
    message: LarkInboundMessage
    suppress_guidance: bool


@dataclass(frozen=True, slots=True)
class _InboundCompleted:
    correlation_id: str
    generation: int
    event_id: str
    message_id: str
    outcome: InboundOutcome
    fatal: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _CancelInbound:
    correlation_id: str


@dataclass(frozen=True, slots=True)
class _InboundResponse:
    outcome: InboundOutcome
    fatal: BaseException | None = None


class _InboundAuthority:
    """Generation-stable in-flight custody, mutated only by the actor."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.generation = 0
        self.pending: dict[str, Future[_InboundResponse]] = {}
        self.keys: dict[str, tuple[str, str]] = {}
        self.in_flight: set[str] = set()

    def begin_generation(self) -> int:
        # In-flight effects survive an actor restart.  Retaining their keys is
        # what prevents a second SDK callback from starting duplicate daemon
        # custody while the old generation's effect is still finishing.
        with self._lock:
            self.generation += 1
            return self.generation

    def register_waiter(
        self, correlation_id: str, future: Future[_InboundResponse]
    ) -> None:
        with self._lock:
            self.pending[correlation_id] = future

    def claim(self, correlation_id: str, event_id: str, message_id: str) -> bool:
        keys = (event_id, message_id)
        with self._lock:
            if any(key in self.in_flight for key in keys):
                return False
            self.keys[correlation_id] = keys
            self.in_flight.update(keys)
            return True

    def complete(
        self,
        correlation_id: str,
        outcome: InboundOutcome,
        fatal: BaseException | None = None,
    ) -> None:
        with self._lock:
            future = self.pending.pop(correlation_id, None)
            keys = self.keys.pop(correlation_id, None)
            if keys is not None:
                self.in_flight.difference_update(keys)
        if future is not None and not future.done():
            future.set_result(_InboundResponse(outcome, fatal))

    def fail(self, correlation_id: str, error: BaseException) -> None:
        with self._lock:
            future = self.pending.pop(correlation_id, None)
            keys = self.keys.pop(correlation_id, None)
            if keys is not None:
                self.in_flight.difference_update(keys)
        if future is not None and not future.done():
            future.set_exception(error)

    def abandon_waiter(self, correlation_id: str, error: BaseException) -> None:
        """Fail the caller but retain in-flight keys until its effect settles."""

        with self._lock:
            future = self.pending.pop(correlation_id, None)
        if future is not None and not future.done():
            future.set_exception(error)

    def custody_count(self) -> int:
        with self._lock:
            return len(self.keys)

    def fail_all(self, error: BaseException) -> None:
        with self._lock:
            correlations = tuple(set(self.pending) | set(self.keys))
        for correlation_id in correlations:
            self.fail(correlation_id, error)


class _InboundEffects:
    def __init__(
        self,
        completion: Callable[[_InboundCompleted], None],
        processor: Callable[[LarkInboundMessage, bool], InboundOutcome],
        *,
        capacity: int,
        workers: int,
    ) -> None:
        self._completion = completion
        try:
            self._processor_ref: Callable[
                [], Callable[[LarkInboundMessage, bool], InboundOutcome] | None
            ] = weakref.WeakMethod(processor)  # type: ignore[arg-type]
        except TypeError:
            self._processor_ref = lambda: processor
        self._slots = threading.BoundedSemaphore(capacity)
        self._pool = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="hyprial-lark-inbound-effect",
        )
        self._closing = False

    def submit(
        self,
        *,
        correlation_id: str,
        generation: int,
        message: LarkInboundMessage,
        suppress_guidance: bool,
    ) -> bool:
        if self._closing or not self._slots.acquire(blocking=False):
            self._completion(
                _InboundCompleted(
                    correlation_id,
                    generation,
                    message.event_id,
                    message.message_id,
                    InboundOutcome(status="error", ack_error="inbound-overloaded"),
                )
            )
            return False
        processor = self._processor_ref()
        if processor is None:
            self._slots.release()
            self._completion(
                _InboundCompleted(
                    correlation_id,
                    generation,
                    message.event_id,
                    message.message_id,
                    InboundOutcome(status="error", ack_error="inbound-closing"),
                )
            )
            return False
        future = self._pool.submit(processor, message, suppress_guidance)

        def completed(done: Future[InboundOutcome]) -> None:
            try:
                outcome = done.result()
            except (NameError, ImportError) as error:
                outcome = InboundOutcome(
                    status="error",
                    ack_error="inbound-programming-error",
                )
                # Never retain the effect thread's traceback: it references
                # the adapter, SQLite store and SDK ports and would keep their
                # file descriptors alive through the guardian/logging path.
                fatal: BaseException | None = type(error)(*error.args)
            except BaseException as error:
                outcome = InboundOutcome(
                    status="error",
                    ack_error=type(error).__name__,
                )
                fatal = None
            else:
                fatal = None
            try:
                self._completion(
                    _InboundCompleted(
                        correlation_id,
                        generation,
                        message.event_id,
                        message.message_id,
                        outcome,
                        fatal,
                    )
                )
            finally:
                self._slots.release()

        future.add_done_callback(completed)
        return True

    def close(self) -> None:
        self._closing = True
        self._pool.shutdown(wait=False, cancel_futures=True)


class _InboundHandler:
    def __init__(
        self,
        *,
        generation: int,
        authority: _InboundAuthority,
        effects: _InboundEffects,
    ) -> None:
        self._generation = generation
        self._authority = authority
        self._effects = effects

    def __call__(self, command: object) -> None:
        if isinstance(command, _SubmitInbound):
            self._submit(command)
        elif isinstance(command, _InboundCompleted):
            self._complete(command)
        elif isinstance(command, _CancelInbound):
            self._authority.fail(
                command.correlation_id,
                RuntimeError("Lark inbound operation cancelled"),
            )
        else:
            raise TypeError(f"unsupported Lark inbound command: {type(command).__name__}")

    def _submit(self, command: _SubmitInbound) -> None:
        if not self._authority.claim(
            command.correlation_id,
            command.message.event_id,
            command.message.message_id,
        ):
            self._authority.complete(
                command.correlation_id,
                InboundOutcome(status="duplicate"),
            )
            return
        self._effects.submit(
            correlation_id=command.correlation_id,
            generation=self._generation,
            message=command.message,
            suppress_guidance=command.suppress_guidance,
        )

    def _complete(self, command: _InboundCompleted) -> None:
        # A previous generation's effect is still authoritative for its exact
        # correlation: it retained custody across restart and must release it.
        if command.fatal is not None:
            self._authority.complete(
                command.correlation_id,
                command.outcome,
                command.fatal,
            )
            # The fault happened on the fixed I/O port, not in actor state
            # transition code.  Propagate it to the SDK caller unchanged;
            # crashing the actor here would retain a second traceback and turn
            # one port defect into an unrelated custody restart.
            return
        self._authority.complete(command.correlation_id, command.outcome)


class LarkInboundRuntime:
    """Bounded actor facade used by one Lark worker process."""

    def __init__(
        self,
        name: str,
        processor: Callable[[LarkInboundMessage, bool], InboundOutcome],
        *,
        mailbox_capacity: int = 128,
        effect_capacity: int = 128,
    ) -> None:
        self._runtime = ActorRuntime()
        self._authority = _InboundAuthority()
        self._handle: ActorHandle | None = None
        self._closing = False

        def completion(value: _InboundCompleted) -> None:
            handle = self._handle
            if handle is None:
                self._authority.fail(
                    value.correlation_id,
                    RuntimeError("Lark inbound runtime is stopped"),
                )
                return
            deadline = time.monotonic() + 5.0
            while True:
                admission = self._runtime.tell(handle, value)
                if admission is AdmissionResult.ACCEPTED:
                    return
                if time.monotonic() >= deadline:
                    self._authority.fail(
                        value.correlation_id,
                        RuntimeError("Lark inbound completion custody failed"),
                    )
                    return
                time.sleep(0.01)

        self._effects = _InboundEffects(
            completion,
            processor,
            capacity=effect_capacity,
            workers=1,
        )

        def handler_factory() -> _InboundHandler:
            return _InboundHandler(
                generation=self._authority.begin_generation(),
                authority=self._authority,
                effects=self._effects,
            )

        self._handle = self._runtime.start(
            ActorSpec(
                name=f"lark-inbound:{name}",
                handler_factory=handler_factory,
                mailbox_capacity=mailbox_capacity,
                supervision_profile="external_io",
            )
        )

    def process(
        self,
        message: LarkInboundMessage,
        *,
        suppress_guidance: bool = False,
        timeout: float = 30.0,
    ) -> InboundOutcome:
        handle = self._handle
        if handle is None or self._closing:
            return InboundOutcome(status="error", ack_error="inbound-closing")
        correlation = uuid.uuid4().hex
        result: Future[_InboundResponse] = Future()
        self._authority.register_waiter(correlation, result)
        admission = self._runtime.tell(
            handle,
            _SubmitInbound(correlation, message, suppress_guidance),
        )
        if admission is AdmissionResult.OVERLOADED:
            self._authority.fail(
                correlation,
                RuntimeError("Lark inbound actor mailbox overloaded"),
            )
            return InboundOutcome(status="error", ack_error="inbound-overloaded")
        if admission is AdmissionResult.CLOSED:
            self._authority.fail(
                correlation,
                RuntimeError("Lark inbound actor is closing"),
            )
            return InboundOutcome(status="error", ack_error="inbound-closing")
        try:
            response = result.result(timeout=timeout)
            if response.fatal is not None:
                raise type(response.fatal)(*response.fatal.args) from None
            return response.outcome
        except FutureTimeout:
            # Timeout cleanup is direct and thread-safe so an overloaded actor
            # mailbox cannot leak custody merely by rejecting Cancel.
            self._authority.abandon_waiter(
                correlation,
                RuntimeError("Lark inbound operation timed out"),
            )
            result.cancel()
            return InboundOutcome(status="error", ack_error="inbound-timeout")

    def close(self, timeout: float = 5.0) -> None:
        handle = self._handle
        if handle is None:
            return
        self._closing = True
        deadline = time.monotonic() + max(0.0, timeout)
        while self._authority.custody_count() and time.monotonic() < deadline:
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        if self._authority.custody_count():
            self._authority.fail_all(RuntimeError("Lark inbound drain deadline expired"))
        self._runtime.stop(handle, timeout=max(0.0, deadline - time.monotonic()))
        self._handle = None
        self._effects.close()


__all__ = ["LarkInboundRuntime"]
