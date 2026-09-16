"""Bounded, generation-fenced completion routing at system edges.

Actor-domain command admission is deliberately not represented here.  A caller
registers a one-shot waiter before submitting a typed command and the domain's
event sink publishes the eventual completion.  Exact generation/version
matching is optional for callers which cannot know a child actor generation in
advance; those callers must use a globally unique correlation id.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, Protocol, TypeVar, cast


class CompletionReceipt(StrEnum):
    """Receipt for a completion, not for mailbox admission."""

    COMMITTED = "committed"
    OVERLOADED = "overloaded"
    STALE = "stale"
    CLOSING = "closing"


class CorrelatedEvent(Protocol):
    correlation_id: str
    generation: int
    version: int


EventT = TypeVar("EventT", bound=CorrelatedEvent)


@dataclass(frozen=True, slots=True)
class CorrelationKey:
    correlation_id: str
    attempt_token: str
    generation: int
    versions: frozenset[int]

    def matches(self, event: CorrelatedEvent, attempt_token: str) -> bool:
        return bool(
            event.correlation_id == self.correlation_id
            and attempt_token == self.attempt_token
            and event.generation == self.generation
            and event.version in self.versions
        )


class CorrelationRouterClosed(RuntimeError):
    pass


class CorrelationRouterOverloaded(RuntimeError):
    pass


class CorrelationWaiter(Generic[EventT]):
    def __init__(self, router: CorrelationEventRouter, key: CorrelationKey) -> None:
        self._router = router
        self.key = key
        self._ready = threading.Event()
        self._event: EventT | None = None
        self._cancelled = False
        self._lock = threading.Lock()

    def _commit(self, event: CorrelatedEvent) -> bool:
        with self._lock:
            if self._cancelled or self._ready.is_set():
                return False
            self._event = cast(EventT, event)
            self._ready.set()
            return True

    def wait(self, timeout: float) -> EventT:
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        if not self._ready.wait(timeout):
            self.cancel()
            raise TimeoutError(
                f"completion deadline elapsed for {self.key.correlation_id}"
            )
        return cast(EventT, self._event)

    def cancel(self) -> None:
        with self._lock:
            if self._ready.is_set() or self._cancelled:
                return
            self._cancelled = True
        self._router._cancel(self.key, self)


class CorrelationEventRouter:
    """Thread-safe bounded registry for one-shot completion waiters."""

    def __init__(self, *, capacity: int = 256) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._lock = threading.Lock()
        self._waiters: dict[CorrelationKey, CorrelationWaiter[CorrelatedEvent]] = {}
        self._closed = False

    def register(
        self,
        correlation_id: str,
        *,
        attempt_token: str,
        generation: int,
        versions: frozenset[int],
    ) -> CorrelationWaiter[CorrelatedEvent]:
        correlation_id = correlation_id.strip()
        if not correlation_id:
            raise ValueError("correlation_id must not be blank")
        attempt_token = attempt_token.strip()
        if not attempt_token:
            raise ValueError("attempt_token must not be blank")
        if generation < 0:
            raise ValueError("generation must not be negative")
        if not versions or any(version < 0 for version in versions):
            raise ValueError("versions must contain non-negative values")
        key = CorrelationKey(correlation_id, attempt_token, generation, versions)
        waiter: CorrelationWaiter[CorrelatedEvent] = CorrelationWaiter(self, key)
        with self._lock:
            if self._closed:
                raise CorrelationRouterClosed("correlation router is closing")
            if len(self._waiters) >= self._capacity:
                raise CorrelationRouterOverloaded("correlation router is full")
            if any(item.correlation_id == correlation_id for item in self._waiters):
                raise ValueError(f"correlation already registered: {correlation_id}")
            self._waiters[key] = waiter
        return waiter

    def publish(
        self,
        event: CorrelatedEvent,
        *,
        attempt_token: str | None = None,
    ) -> CompletionReceipt:
        """Commit an exact completion to one waiter.

        A late or generation/version-mismatched event is explicitly stale.  It
        can never wake a later operation reusing the same human-readable id.
        """

        if not all(
            hasattr(event, field)
            for field in ("correlation_id", "generation", "version")
        ):
            raise TypeError("completion must carry correlation/generation/version")
        if getattr(event, "admission", None) is not None:
            # Actor ports may publish typed diagnostics for OVERLOADED/CLOSING
            # admission.  They are explicitly not mutation completions.
            return CompletionReceipt.STALE
        event_attempt = attempt_token or str(getattr(event, "attempt_token", ""))
        if not event_attempt and event.correlation_id.startswith("lifecycle:"):
            event_attempt = event.correlation_id.removeprefix("lifecycle:")
        if not event_attempt:
            return CompletionReceipt.STALE
        with self._lock:
            if self._closed:
                return CompletionReceipt.CLOSING
            match = next(
                (
                    (key, waiter)
                    for key, waiter in self._waiters.items()
                    if key.matches(event, event_attempt)
                ),
                None,
            )
            if match is None:
                return CompletionReceipt.STALE
            key, waiter = match
            del self._waiters[key]
        return (
            CompletionReceipt.COMMITTED
            if waiter._commit(event)
            else CompletionReceipt.STALE
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            waiters = tuple(self._waiters.values())
            self._waiters.clear()
        for waiter in waiters:
            waiter.cancel()

    def pending(self) -> int:
        with self._lock:
            return len(self._waiters)

    def _cancel(
        self,
        key: CorrelationKey,
        waiter: CorrelationWaiter[CorrelatedEvent],
    ) -> None:
        with self._lock:
            if self._waiters.get(key) is waiter:
                del self._waiters[key]
