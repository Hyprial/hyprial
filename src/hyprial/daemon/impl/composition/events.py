"""Correlated domain event bus and domain command errors shared by the composition ports."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import threading
import time
from collections import OrderedDict
from typing import TypeVar, cast
from hyprial.kernel import PortCommandRejected
from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import (
    LifecycleReceiptCompleted,
)


_AGENT_RECEIPT_NAMESPACE = "agent:receipt"


class DomainCommandError(RuntimeError):
    """A typed domain command was rejected or did not settle in budget."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _check_domain_receipt(
    event: LifecycleReceiptCompleted,
    domain: str,
    generation: int,
    attempt_token: str,
    resource_token: str,
    operation: str,
) -> None:
    if (
        event.domain != domain
        or event.generation != generation
        or event.attempt_token != attempt_token
        or event.resource_token != resource_token
        or event.operation != operation
    ):
        raise DomainCommandError(
            "STALE_LIFECYCLE_RECEIPT", f"{domain} receipt completion fence mismatch"
        )


class CorrelatedDomainEvents:
    """Bounded correlated event journal for synchronous system edges only."""

    def __init__(self, *, capacity: int = 4096) -> None:
        if capacity < 1:
            raise ValueError("event capacity must be positive")
        self._capacity = capacity
        self._condition = threading.Condition()
        self._events: OrderedDict[str, list[object]] = OrderedDict()
        self._settlement_reservations: set[str] = set()
        self._closed = False

    def reserve_settlement(self, correlation_id: str) -> None:
        with self._condition:
            if self._closed:
                raise DomainCommandError(
                    "DOMAIN_EVENTS_CLOSED", "domain event owner is closed"
                )
            if correlation_id in self._settlement_reservations:
                raise DomainCommandError(
                    "DOMAIN_CORRELATION_CONFLICT",
                    f"domain correlation is already reserved: {correlation_id}",
                )
            if len(self._settlement_reservations) >= self._capacity:
                raise DomainCommandError(
                    "DOMAIN_EVENT_CAPACITY",
                    "domain settlement reservation capacity is full",
                )
            self._settlement_reservations.add(correlation_id)

    def cancel_settlement(self, correlation_id: str) -> None:
        with self._condition:
            self._settlement_reservations.discard(correlation_id)
            self._events.pop(correlation_id, None)
            self._condition.notify_all()

    def publish(self, event: object) -> None:
        correlation_id = getattr(event, "correlation_id", None)
        if not isinstance(correlation_id, str) or not correlation_id:
            raise TypeError("domain event must carry a non-empty correlation_id")
        with self._condition:
            if self._closed:
                return
            self._events.setdefault(correlation_id, []).append(event)
            self._events.move_to_end(correlation_id)
            while len(self._events) > self._capacity:
                evictable = next(
                    (
                        current
                        for current in self._events
                        if current not in self._settlement_reservations
                    ),
                    None,
                )
                if evictable is None:
                    break
                self._events.pop(evictable, None)
            self._condition.notify_all()

    def wait(
        self,
        correlation_id: str,
        expected: type[_EventT],
        *,
        timeout: float,
    ) -> _EventT:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                events = self._events.get(correlation_id, [])
                for event in events:
                    if isinstance(event, PortCommandRejected):
                        self._events.pop(correlation_id, None)
                        raise DomainCommandError(event.code, event.detail)
                    if isinstance(event, expected):
                        self._events.pop(correlation_id, None)
                        return cast(_EventT, event)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DomainCommandError(
                        "DOMAIN_COMMAND_TIMEOUT",
                        f"domain command {correlation_id} did not settle",
                    )
                self._condition.wait(remaining)

    def wait_settled(
        self,
        correlation_id: str,
        expected: type[_EventT],
    ) -> _EventT:
        """Join one admitted command without turning a deadline into cancel."""

        with self._condition:
            while True:
                events = self._events.get(correlation_id, [])
                for event in events:
                    if isinstance(event, PortCommandRejected):
                        self._events.pop(correlation_id, None)
                        self._settlement_reservations.discard(correlation_id)
                        raise DomainCommandError(event.code, event.detail)
                    if isinstance(event, expected):
                        self._events.pop(correlation_id, None)
                        self._settlement_reservations.discard(correlation_id)
                        return cast(_EventT, event)
                if self._closed:
                    self._settlement_reservations.discard(correlation_id)
                    raise DomainCommandError(
                        "DOMAIN_EVENTS_CLOSED",
                        f"domain command {correlation_id} lost its event owner",
                    )
                self._condition.wait()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._events.clear()
            self._settlement_reservations.clear()
            self._condition.notify_all()


_EventT = TypeVar("_EventT")
