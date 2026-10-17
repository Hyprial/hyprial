"""HookInterceptPort: the bus's bounded, fail-open intercept side.

Observe lanes fan facts out; the intercept port is the one place a hook may
*contribute* to a delivery.  The caller (a delivery pump) polls ``prepare``
every tick and must never block on a handler: the first call starts one
bounded invocation on its own thread, later calls read the recorded outcome.

Fail-open is the only failure mode.  A timeout, an invoker error, an empty
reply, a reply that lands after the deadline, a full invocation pool and a
closing port all produce the same decision: proceed with the original value
and no text.  Only a non-empty reply that arrived inside the deadline
produces text, and that text is returned verbatim -- the port never parses,
trims or rewrites it.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_CONCURRENT_INTERCEPTS = 32

#: One bounded request/reply exchange; raises or returns the reply text.
InterceptInvoker = Callable[..., "str | None"]


@dataclass(frozen=True, slots=True)
class InterceptRequest:
    """One delivery offered to one configured intercept handler.

    ``document`` may be the request mapping itself or a zero-argument
    factory.  A factory is resolved on the invocation thread, inside the
    bounded exchange, so building the request (which may gather context
    from disk) never runs on the caller's delivery pump; a factory that
    raises fails open like any other invocation error.
    """

    key: str
    owner: str
    handler: str
    timeout_ms: int
    document: Mapping[str, Any] | Callable[[], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class InterceptDecision:
    """``ready`` False holds the value; True proceeds, with optional text."""

    ready: bool
    text: str | None
    outcome: str  # pending | text | empty | timeout | error | closed


_HOLD = InterceptDecision(ready=False, text=None, outcome="pending")


@dataclass(slots=True)
class _PendingIntercept:
    owner: str
    deadline: float
    outcome: queue.Queue[tuple[str, object, float]]
    decided: InterceptDecision | None = None
    dispatched: bool = False


class HookInterceptPort:
    """Per-key intercept state machine with at-most-one invocation per key."""

    def __init__(
        self,
        *,
        invoke: InterceptInvoker,
        logger: Callable[..., None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_INTERCEPTS,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("intercept concurrency must be at least 1")
        self._invoke = invoke
        self._logger = logger
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingIntercept] = {}
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._threads: set[threading.Thread] = set()
        self._closed = False

    def prepare(self, request: InterceptRequest) -> InterceptDecision:
        """Advance one key's intercept; never blocks on the handler."""

        if request.timeout_ms < 1:
            raise ValueError("intercept timeout_ms must be positive")
        start = False
        with self._lock:
            pending = self._pending.get(request.key)
            if pending is None:
                if self._closed:
                    return InterceptDecision(True, None, "closed")
                pending = _PendingIntercept(
                    owner=request.owner,
                    deadline=self._monotonic() + request.timeout_ms / 1000,
                    outcome=queue.Queue(maxsize=1),
                )
                self._pending[request.key] = pending
                start = True
        if start:
            # The pending slot is reserved; starting the invocation may spawn
            # a thread, so it happens outside the lock.  Every later caller
            # observes the same in-flight exchange without waiting here.
            self._start_invocation(request, pending.outcome)
            return _HOLD
        with self._lock:
            pending = self._pending.get(request.key)
            if pending is None:
                return InterceptDecision(True, None, "empty")
            if pending.decided is not None:
                return pending.decided
            if pending.dispatched:
                # The caller already dispatched an accepted value for this
                # key; a late poll must repeat the fail-open original rather
                # than hand a second, different answer to a retry.
                pending.decided = InterceptDecision(True, None, "empty")
                return pending.decided
            try:
                kind, value, completed_at = pending.outcome.get_nowait()
            except queue.Empty:
                if self._monotonic() < pending.deadline:
                    return _HOLD
                pending.decided = InterceptDecision(True, None, "timeout")
            else:
                pending.decided = self._decide(kind, value, completed_at, pending)
            decided = pending.decided
        self._log_outcome(request, decided)
        return decided

    @staticmethod
    def _decide(
        kind: str,
        value: object,
        completed_at: float,
        pending: _PendingIntercept,
    ) -> InterceptDecision:
        if completed_at > pending.deadline:
            # The reply exists but missed its budget; honouring it would make
            # delivery latency depend on a handler that already timed out.
            return InterceptDecision(True, None, "timeout")
        if kind == "text" and isinstance(value, str) and value:
            return InterceptDecision(True, value, "text")
        if kind == "text":
            return InterceptDecision(True, None, "empty")
        if kind == "timeout":
            return InterceptDecision(True, None, "timeout")
        return InterceptDecision(True, None, "error")

    def _start_invocation(
        self,
        request: InterceptRequest,
        outcome: queue.Queue[tuple[str, object, float]],
    ) -> None:
        if not self._slots.acquire(blocking=False):
            outcome.put_nowait(
                (
                    "error",
                    RuntimeError("intercept invocation capacity full"),
                    self._monotonic(),
                )
            )
            if self._logger is not None:
                self._logger(
                    "warn",
                    "daemon",
                    "hooks.intercept_overloaded",
                    deliveryId=request.key,
                    handler=request.handler,
                )
            return

        def invoke() -> None:
            try:
                try:
                    document = (
                        request.document()
                        if callable(request.document)
                        else request.document
                    )
                    text = self._invoke(
                        request.handler,
                        document,
                        timeout_seconds=request.timeout_ms / 1000,
                    )
                except TimeoutError as error:
                    outcome.put(("timeout", error, self._monotonic()))
                except Exception as error:  # noqa: BLE001 - intercept fails open
                    outcome.put(("error", error, self._monotonic()))
                else:
                    outcome.put(("text", text, self._monotonic()))
            finally:
                self._slots.release()
                with self._lock:
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(
            target=invoke,
            name=f"hyprial-intercept-{request.key[:12]}",
            daemon=True,
        )
        with self._lock:
            self._threads.add(thread)
        thread.start()

    def _log_outcome(
        self, request: InterceptRequest, decision: InterceptDecision
    ) -> None:
        if self._logger is None or decision.outcome == "pending":
            return
        self._logger(
            "info" if decision.outcome in {"text", "empty"} else "warn",
            "daemon",
            f"hooks.intercept_{decision.outcome}",
            deliveryId=request.key,
            agent=request.owner,
            handler=request.handler,
        )

    def mark_dispatched(self, key: str) -> None:
        """Record that the caller dispatched this key's accepted value."""

        with self._lock:
            pending = self._pending.get(key)
            if pending is not None:
                pending.dispatched = True

    def forget(self, key: str) -> None:
        """Release one key once its delivery settled terminally."""

        with self._lock:
            self._pending.pop(key, None)

    def forget_missing(self, owner: str, present_keys: frozenset[str]) -> None:
        """Release an owner's keys whose rows were acknowledged elsewhere."""

        with self._lock:
            missing = tuple(
                key
                for key, pending in self._pending.items()
                if pending.owner == owner and key not in present_keys
            )
            for key in missing:
                self._pending.pop(key, None)

    def close(self, timeout: float = 5.0) -> bool:
        """Refuse new intercepts and wait for in-flight invocation threads."""

        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            self._closed = True
            threads = tuple(self._threads)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            return not any(thread.is_alive() for thread in self._threads)


__all__ = [
    "DEFAULT_MAX_CONCURRENT_INTERCEPTS",
    "HookInterceptPort",
    "InterceptDecision",
    "InterceptRequest",
]
