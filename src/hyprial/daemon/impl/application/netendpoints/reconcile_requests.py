"""Reply-before-reconcile bookkeeping for explicit org membership operations.

An ``org.*`` IPC handler (invite, join, remove, ...) replies as soon as its
write is durable and hands the forwarding reconcile -- a membership read and
the slot rebinding, both through the serial sidecar I/O owner -- to the
daemon's ``forwarding-reconcile`` cadence instead of running it on the
request thread (0.5.5.1 hotfix, 2026-10-28: the reconcile stalled past the
CLI's 15s budget while every invite had already been created).

The cadence coalesces ticks, so a completion is attributed by time: every
request recorded at or before the tick's observation was covered by that
tick's reconcile and settles with one ``org.<op>.reconcile`` event carrying
``ok`` / ``code`` / ``durationMs`` plus the request's own fields.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from hyprial.daemon.impl.correlation.bounded_cadence import CadenceCompleted

_Log = Callable[..., None]


class ReconcileWaiter:
    """Set by the run that settles its request; carries that run's outcome."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.ok = False
        #: The failed run's typed ``.code`` only; never an exception type name.
        self.code: str | None = None
        #: Diagnostic text for the message (``Type: detail``); never a code.
        self.detail: str | None = None


@dataclass(frozen=True, slots=True)
class ReconcileRequest:
    reason: str
    fields: Mapping[str, Any]
    requested_at_ms: int
    waiter: ReconcileWaiter | None = None

    @property
    def event(self) -> str:
        return f"org.{self.reason.removeprefix('org-')}.reconcile"


class ForwardingReconcileRequests:
    """Pending explicit-operation reconciles, settled by cadence completions."""

    def __init__(
        self,
        log: _Log,
        *,
        capacity: int = 64,
        now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        if capacity < 1:
            raise ValueError("reconcile request capacity must be at least 1")
        self._log = log
        self._capacity = capacity
        self._now_ms = now_ms
        self._lock = threading.Lock()
        self._requests: list[ReconcileRequest] = []
        self._last_observation_ms = 0
        #: Serializes on-demand creation of the forwarding cadence (IPC thread
        #: vs. maintenance start): one actor, never two.
        self.guard = threading.Lock()

    def observation(self) -> int:
        """A tick observation time, strictly after every earlier one.

        Ticks (maintenance) and requests draw from this one source, so a
        request recorded after a tick was submitted sorts strictly after it
        even within one millisecond: a run that may have read state before
        the request never settles it.
        """

        with self._lock:
            return self._next_observation_locked()

    def _next_observation_locked(self) -> int:
        self._last_observation_ms = max(int(self._now_ms()), self._last_observation_ms + 1)
        return self._last_observation_ms

    def record(
        self, reason: str, *, waiter: ReconcileWaiter | None = None, **fields: Any
    ) -> int:
        """Record one request; returns the observation time to submit the tick with."""

        dropped: ReconcileRequest | None = None
        with self._lock:
            request = ReconcileRequest(
                reason, dict(fields), self._next_observation_locked(), waiter
            )
            self._requests.append(request)
            if len(self._requests) > self._capacity:
                dropped = self._requests.pop(0)
        if dropped is not None:
            # The reconcile still happens on the next tick; only the
            # attribution of the oldest request is lost, and loudly.
            self._log(
                "warn", "org", "org.reconcile.requests_overflow",
                capacity=self._capacity,
                dropped={"reason": dropped.reason, **dropped.fields},
            )
        return request.requested_at_ms

    def pending(self) -> int:
        with self._lock:
            return len(self._requests)

    def settle(self, completion: CadenceCompleted[Any]) -> tuple[ReconcileRequest, ...]:
        """Settle every request the completed tick covered; log one event each."""

        with self._lock:
            covered = tuple(
                item for item in self._requests
                if item.requested_at_ms <= completion.observed_at_ms
            )
            if covered:
                self._requests = [
                    item for item in self._requests
                    if item.requested_at_ms > completion.observed_at_ms
                ]
        ok = completion.error is None
        code = None if ok else (completion.code or completion.error)
        for item in covered:
            if item.waiter is not None:
                item.waiter.ok = ok
                item.waiter.code = None if ok else completion.code
                item.waiter.detail = None if ok else (
                    f"{completion.error}: {completion.detail}"
                    if completion.detail and completion.detail != completion.error
                    else completion.error
                )
                item.waiter.done.set()
            self._log(
                "info" if ok else "warn", "org", item.event,
                ok=ok, code=code, durationMs=completion.duration_ms, **item.fields,
            )
        return covered


__all__ = ["ForwardingReconcileRequests", "ReconcileRequest", "ReconcileWaiter"]
