from __future__ import annotations
import threading
import time
from collections import deque
from dataclasses import dataclass
from hyprial.daemon.impl.inbox.contracts.api  import (
    SubmissionResult,
)
from hyprial.daemon.impl.inbox.contracts.ports  import (
    SubmissionProjection,
)

from ..events import (
    CompletionReceiptClaim,
    CompletionReceiptState,
    DispatchIoRequested,
)


class _CompletionReceipts:
    """Bounded actor-processed receipts; mailbox admission is not settlement."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._condition = threading.Condition()
        self._states: dict[str, CompletionReceiptState] = {}
        self._generations: dict[str, int] = {}
        self._order: deque[str] = deque()

    def claim(self, token: str, generation: int) -> CompletionReceiptClaim:
        with self._condition:
            state = self._states.get(token)
            if state is CompletionReceiptState.RETRY:
                self._states[token] = CompletionReceiptState.PENDING
                self._generations[token] = generation
                return CompletionReceiptClaim.NEW
            if state is CompletionReceiptState.PENDING:
                if self._generations[token] != generation:
                    self._generations[token] = generation
                    return CompletionReceiptClaim.NEW
                return CompletionReceiptClaim.WAIT
            if state is CompletionReceiptState.SETTLED:
                return CompletionReceiptClaim.SETTLED
            while len(self._states) >= self._capacity and self._order:
                oldest = self._order[0]
                if self._states[oldest] is CompletionReceiptState.PENDING:
                    return CompletionReceiptClaim.FULL
                self._order.popleft()
                self._states.pop(oldest, None)
                self._generations.pop(oldest, None)
            if len(self._states) >= self._capacity:
                return CompletionReceiptClaim.FULL
            self._states[token] = CompletionReceiptState.PENDING
            self._generations[token] = generation
            self._order.append(token)
            return CompletionReceiptClaim.NEW

    def acknowledge(self, token: str, state: CompletionReceiptState) -> None:
        with self._condition:
            if token not in self._states:
                return
            self._states[token] = state
            self._condition.notify_all()

    def wait(self, token: str, timeout: float) -> CompletionReceiptState | None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._states.get(token) is CompletionReceiptState.PENDING:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return self._states.get(token)

    def is_settled(self, token: str) -> bool:
        with self._condition:
            return self._states.get(token) is CompletionReceiptState.SETTLED
@dataclass(frozen=True, slots=True)
class _PendingDispatch:
    request: DispatchIoRequested
    completion_kind: str
    pre_results: tuple[SubmissionResult, ...] = ()
    # At most one timestamp per message already owned by this dispatch. A wake
    # must survive the older I/O result that can otherwise reintroduce backoff.
    online_wakes: tuple[tuple[str, int], ...] = ()
@dataclass(frozen=True, slots=True)
class _DurableSubmissionReceipt:
    command_digest: str
    result: SubmissionProjection
class _SubmissionReceiptConflict(RuntimeError):
    pass
class _DurableCompletionHandoffs:
    """Bounded cross-thread notices backed by already-persisted outbox rows."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._lock = threading.Lock()
        self._correlations: set[str] = set()

    def record(self, correlation_id: str) -> bool:
        with self._lock:
            if correlation_id in self._correlations:
                return True
            if len(self._correlations) >= self._capacity:
                return False
            self._correlations.add(correlation_id)
            return True

    def consume_all(self) -> tuple[str, ...]:
        with self._lock:
            correlations = tuple(self._correlations)
            self._correlations.clear()
            return correlations
