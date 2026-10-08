from __future__ import annotations
import threading
import time
from hyprial.kernel import PortCommandRejected
from hyprial.daemon.impl.inbox.contracts.api  import (
    InboxMessage,
)
from hyprial.daemon.impl.inbox.contracts.ports  import (
    InboxCountsProjection,
    InboxEventSink,
    InboxProjectionPort,
)

from ..errors import InboxAuthorityTimeout

class _SystemReply:
    def __init__(self, correlation_id: str, expected: type[object]) -> None:
        self.correlation_id = correlation_id
        self.expected = expected
        self._condition = threading.Condition()
        self._event: object | None = None

    def offer(self, event: object) -> bool:
        if not isinstance(event, (self.expected, PortCommandRejected)):
            return False
        with self._condition:
            if self._event is None:
                self._event = event
                self._condition.notify_all()
        return True

    def wait(self, timeout: float) -> object:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._event is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InboxAuthorityTimeout(
                        f"inbox command timed out: {self.correlation_id}"
                    )
                self._condition.wait(remaining)
            return self._event
class _EventFanout:
    def __init__(self, primary: InboxEventSink) -> None:
        self._primary = primary
        self._lock = threading.Lock()
        self._replies: dict[str, _SystemReply] = {}

    def expect(self, correlation_id: str, expected: type[object]) -> _SystemReply:
        reply = _SystemReply(correlation_id, expected)
        with self._lock:
            if correlation_id in self._replies:
                raise ValueError(f"duplicate synchronous correlation: {correlation_id}")
            self._replies[correlation_id] = reply
        return reply

    def cancel(self, correlation_id: str) -> None:
        with self._lock:
            self._replies.pop(correlation_id, None)

    def publish(self, event: object) -> None:
        try:
            self._primary.publish(event)  # type: ignore[arg-type]
        finally:
            correlation_id = getattr(event, "correlation_id", None)
            if isinstance(correlation_id, str):
                with self._lock:
                    reply = self._replies.get(correlation_id)
                if reply is not None and reply.offer(event):
                    self.cancel(correlation_id)
class _ProjectionState(InboxProjectionPort):
    """Atomically swapped immutable read face; readers never touch actor state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Every committed inbox mutation publishes here (``replace``), so a
        # waiter on this condition hears each change without polling SQLite.
        self._changed = threading.Condition(self._lock)
        self._counts = InboxCountsProjection(
            version=0,
            outbox_count=0,
            custody_count=0,
        )
        self._pending: tuple[InboxMessage, ...] = ()
        self._pending_work: frozenset[str] = frozenset()
        self._claimed_message_ids: frozenset[str] = frozenset()

    @property
    def version(self) -> int:
        with self._lock:
            return self._counts.version

    def replace(
        self,
        *,
        version: int,
        outbox_count: int,
        custody_count: int,
        pending: tuple[InboxMessage, ...],
        pending_work: frozenset[str],
    ) -> None:
        counts = InboxCountsProjection(
            version=version,
            outbox_count=outbox_count,
            custody_count=custody_count,
        )
        with self._lock:
            self._counts = counts
            self._pending = pending
            self._pending_work = pending_work
            self._changed.notify_all()

    def advance_closed(self, version: int) -> None:
        with self._lock:
            self._counts = InboxCountsProjection(
                version=version,
                outbox_count=self._counts.outbox_count,
                custody_count=self._counts.custody_count,
            )
            self._changed.notify_all()

    def wait_for_version(self, version: int, timeout: float) -> int:
        """Block until the projection moves past ``version`` or ``timeout``.

        Returns the current version either way; the caller compares.
        """

        with self._changed:
            self._changed.wait_for(
                lambda: self._counts.version != version, timeout=max(0.0, timeout)
            )
            return self._counts.version

    def read_counts(self) -> InboxCountsProjection:
        with self._lock:
            return self._counts

    def replace_claimed(self, message_ids: frozenset[str]) -> None:
        with self._lock:
            self._claimed_message_ids = message_ids

    def read_claimed_message_ids(self) -> frozenset[str]:
        with self._lock:
            return self._claimed_message_ids

    def read_pending(self, recipient: str) -> tuple[InboxMessage, ...]:
        with self._lock:
            return tuple(
                message for message in self._pending if message.recipient == recipient
            )

    def has_pending_work(self, recipient: str) -> bool:
        with self._lock:
            return recipient in self._pending_work
