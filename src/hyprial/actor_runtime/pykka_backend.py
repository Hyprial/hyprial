from __future__ import annotations

import threading
from dataclasses import dataclass, field

import pykka

from .contracts import (
    ActorBackend,
    ActorEvent,
    ActorEventKind,
    ActorHandle,
    AdmissionResult,
    CommandStartedSink,
    CommandHandler,
    EventSink,
    ExpectedActorError,
    FailureSink,
)


@dataclass(frozen=True, slots=True)
class _CommandEnvelope:
    custody_token: str
    command: object


class _MailboxGate:
    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._lock = threading.Lock()
        self._queued = 0
        self._in_flight = 0
        self._closed = False

    def reserve(self) -> AdmissionResult:
        with self._lock:
            if self._closed:
                return AdmissionResult.CLOSED
            if self._queued >= self._capacity:
                return AdmissionResult.OVERLOADED
            self._queued += 1
            return AdmissionResult.ACCEPTED

    def cancel_reservation(self) -> None:
        with self._lock:
            self._queued = max(0, self._queued - 1)

    def begin(self) -> None:
        with self._lock:
            self._queued = max(0, self._queued - 1)
            self._in_flight += 1

    def finish(self) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)

    def close(self, *, discard_queued: bool = False) -> None:
        with self._lock:
            self._closed = True
            if discard_queued:
                self._queued = 0

    def load(self) -> tuple[int, int]:
        with self._lock:
            return self._queued, self._in_flight


class _RuntimeActor(pykka.ThreadingActor):
    use_daemon_thread = True

    def __init__(
        self,
        *,
        handle: ActorHandle,
        generation: int,
        handler: CommandHandler,
        gate: _MailboxGate,
        event_sink: EventSink,
        failure_callback: FailureSink,
        command_started: CommandStartedSink,
    ) -> None:
        super().__init__()
        self._handle = handle
        self._generation = generation
        self._handler = handler
        self._gate = gate
        self._event_sink = event_sink
        self._failure_callback = failure_callback
        self._command_started = command_started

    def on_receive(self, message: object) -> None:
        if not isinstance(message, _CommandEnvelope):
            raise TypeError(f"unsupported runtime envelope: {type(message).__name__}")
        self._gate.begin()
        self._command_started(
            self._handle.actor_id,
            self._generation,
            message.custody_token,
        )
        command_type = type(message.command).__name__
        try:
            self._handler(message.command)
        except ExpectedActorError as exc:
            self._safe_emit(
                ActorEvent(
                    kind=ActorEventKind.COMMAND_REJECTED,
                    handle=self._handle,
                    generation=self._generation,
                    command_type=command_type,
                    code=exc.code,
                    detail=exc.detail,
                )
            )
        else:
            self._safe_emit(
                ActorEvent(
                    kind=ActorEventKind.COMMAND_COMPLETED,
                    handle=self._handle,
                    generation=self._generation,
                    command_type=command_type,
                )
            )
        finally:
            self._gate.finish()

    def on_failure(
        self,
        exception_type: type[BaseException] | None,
        exception_value: BaseException | None,
        traceback: object,
    ) -> None:
        del exception_type, traceback
        self._gate.close(discard_queued=True)
        failure = exception_value or RuntimeError("actor failed without an exception")
        try:
            self._failure_callback(
                self._handle.actor_id,
                self._generation,
                failure,
            )
        except Exception:
            return

    def _safe_emit(self, event: ActorEvent) -> None:
        try:
            self._event_sink(event)
        except Exception:
            return


@dataclass(slots=True)
class _BackendEndpoint:
    ref: pykka.ActorRef[_RuntimeActor]
    gate: _MailboxGate
    stop_guard: threading.Lock = field(default_factory=threading.Lock)
    stop_future: pykka.Future[bool] | None = None


class _PykkaBackend:
    """The only module allowed to import or expose Pykka internally."""

    def start(
        self,
        *,
        handle: ActorHandle,
        generation: int,
        handler: CommandHandler,
        mailbox_capacity: int,
        event_sink: EventSink,
        failure_callback: FailureSink,
        command_started: CommandStartedSink,
    ) -> object:
        gate = _MailboxGate(mailbox_capacity)
        ref = _RuntimeActor.start(
            handle=handle,
            generation=generation,
            handler=handler,
            gate=gate,
            event_sink=event_sink,
            failure_callback=failure_callback,
            command_started=command_started,
        )
        return _BackendEndpoint(ref=ref, gate=gate)

    def tell(
        self, endpoint: object, custody_token: str, command: object
    ) -> AdmissionResult:
        endpoint = self._require_endpoint(endpoint)
        admission = endpoint.gate.reserve()
        if admission is not AdmissionResult.ACCEPTED:
            return admission
        try:
            endpoint.ref.tell(_CommandEnvelope(custody_token, command))
        except pykka.ActorDeadError:
            endpoint.gate.cancel_reservation()
            endpoint.gate.close(discard_queued=True)
            return AdmissionResult.CLOSED
        return AdmissionResult.ACCEPTED

    def close_admission(self, endpoint: object) -> None:
        endpoint = self._require_endpoint(endpoint)
        endpoint.gate.close()

    def load(self, endpoint: object) -> tuple[int, int]:
        endpoint = self._require_endpoint(endpoint)
        return endpoint.gate.load()

    def stop(self, endpoint: object, timeout: float) -> bool:
        endpoint = self._require_endpoint(endpoint)
        endpoint.gate.close()
        if not endpoint.ref.is_alive():
            return True
        try:
            with endpoint.stop_guard:
                if endpoint.stop_future is None:
                    endpoint.stop_future = endpoint.ref.stop(block=False)
                future = endpoint.stop_future
            stopped = bool(future.get(timeout=max(0.0, timeout)))
        except pykka.Timeout:
            return False
        return stopped or not endpoint.ref.is_alive()

    @staticmethod
    def _require_endpoint(endpoint: object) -> _BackendEndpoint:
        if not isinstance(endpoint, _BackendEndpoint):
            raise TypeError("endpoint was not created by PykkaBackend")
        return endpoint


def create_backend() -> ActorBackend:
    return _PykkaBackend()
