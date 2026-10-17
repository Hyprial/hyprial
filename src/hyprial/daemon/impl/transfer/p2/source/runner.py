"""Volatile source-effect custody; durable migration facts stay in P2 journal.

The mailbox only admits/attaches observers and acknowledges typed completion.
Neither SQLite nor lifecycle/harness waits run here. A caller's cancellation
does not cancel an accepted effect, and shutdown drains before stopping it.
"""

from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass
import threading
import time

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import EffectLane, EffectRequest, EffectCompleted
from ..actor import _finish
from .effect import QuiesceRequest

SOURCE_EFFECT_CAPACITY = 32


@dataclass(frozen=True, slots=True)
class _Submit:
    request: QuiesceRequest
    result: Future


@dataclass(frozen=True, slots=True)
class _Drain:
    done: Future


class SourceRunner:
    def __init__(self, execute, *, capacity=SOURCE_EFFECT_CAPACITY):
        self._capacity = capacity
        self._pending: dict[str, tuple[QuiesceRequest, int, list[Future]]] = {}
        self._generation = 0
        self._admission = threading.Lock()
        self._closing = None
        self._drain = None
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(ActorSpec(
            name='transfer-source-effects', handler_factory=lambda: self._receive,
            mailbox_capacity=capacity, undelivered_sink=self._undelivered,
        ))
        self._effects = EffectLane(
            name='transfer-source-execute', execute=execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity, workers=1,
        )

    def submit(self, request: QuiesceRequest) -> Future:
        if not isinstance(request, QuiesceRequest):
            raise TypeError('source runner requires QuiesceRequest')
        future = Future()
        with self._admission:
            if self._closing is not None:
                _finish(future, error=RuntimeError('source runner is closing'))
            elif self._runtime.tell(self._handle, _Submit(request, future)) is not AdmissionResult.ACCEPTED:
                _finish(future, error=RuntimeError('source mailbox not admitted'))
        return future

    @staticmethod
    def _undelivered(message, reason):
        if isinstance(message, _Submit):
            _finish(message.result, error=RuntimeError(f'source not delivered: {reason}'))
        elif isinstance(message, _Drain):
            _finish(message.done, error=RuntimeError(f'source drain not delivered: {reason}'))

    def _receive(self, message):
        if isinstance(message, _Submit):
            request, future = message.request, message.result
            previous = self._pending.get(request.operation_id)
            if previous is not None and previous[0] != request:
                _finish(future, error=ValueError('source operation fingerprint mismatch'))
            elif sum(len(waiters) for _, _, waiters in self._pending.values()) >= self._capacity:
                _finish(future, error=RuntimeError('source observer capacity'))
            elif previous is not None:
                previous[2].append(future)
            else:
                self._generation += 1
                self._pending[request.operation_id] = (request, self._generation, [future])
                if self._effects.submit(EffectRequest(request.operation_id, self._generation, request)) is not AdmissionResult.ACCEPTED:
                    self._pending.pop(request.operation_id)
                    _finish(future, error=RuntimeError('source effect not admitted'))
        elif isinstance(message, EffectCompleted):
            previous = self._pending.get(message.operation_id)
            if previous is not None and previous[1] == message.generation:
                self._pending.pop(message.operation_id)
                for future in previous[2]:
                    if message.error is not None:
                        _finish(future, error=RuntimeError(f'source outcome unknown: {message.error}'))
                    else:
                        _finish(future, message.result)
            # Duplicate completion is harmless; the lane retains its result
            # until this authority explicitly acknowledges its generation.
            self._effects.acknowledge(message.operation_id, message.generation)
        elif isinstance(message, _Drain):
            self._drain = message.done
        else:
            raise TypeError('unexpected source runner message')
        if self._drain is not None and not self._pending and not self._drain.done():
            _finish(self._drain)

    def close(self, *, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._admission:
            if self._closing is None:
                future = Future()
                if self._runtime.tell(self._handle, _Drain(future)) is not AdmissionResult.ACCEPTED:
                    return False
                self._closing = future
        try:
            self._closing.result(max(0.0, deadline - time.monotonic()))
        except (TimeoutError, RuntimeError):
            return False
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        return self._runtime.stop(self._handle, max(0.0, deadline - time.monotonic()))
