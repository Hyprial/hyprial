"""Cooperative cancellation scoped to one explicitly cancellable request.

Transport disconnect is never a rollback for durable mutations. Callers and
handlers opt in only for the bounded credential probe; nothing is serialized
as caller authority. Context propagation follows the carrier's worker task.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import threading


class RequestCancellation:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._callbacks: dict[object, Callable[[], None]] = {}

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            callbacks = tuple(self._callbacks.values())
            self._callbacks.clear()
        for callback in callbacks:
            callback()

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        key = object()
        with self._lock:
            cancelled = self._cancelled
            if not cancelled:
                self._callbacks[key] = callback
        if cancelled:
            callback()

        def unsubscribe() -> None:
            with self._lock:
                self._callbacks.pop(key, None)

        return unsubscribe


_CURRENT: ContextVar[RequestCancellation | None] = ContextVar('request_cancellation', default=None)


def current_request_cancellation() -> RequestCancellation | None:
    return _CURRENT.get()


@contextmanager
def request_cancellation(cancellation: RequestCancellation) -> Iterator[None]:
    token = _CURRENT.set(cancellation)
    try:
        yield
    finally:
        _CURRENT.reset(token)
