"""Advisory fetch-receipt publication, backed by the durable fetched projection.

The inbox commit is the authority. Failed/overloaded push hints do not undo a
fetch; senders reconcile against the committed receipt queryable (#891).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.daemon.impl.transport import KeySpace


@dataclass(frozen=True, slots=True)
class PublishFetchReceipt:
    sender: str
    message_id: str


@dataclass(frozen=True, slots=True)
class FetchReceiptProjection:
    accepted: int
    published: int
    failed: int
    rejected: int
    closed: bool


class FetchReceiptPublisher:
    def __init__(self, session, *, capacity=128, keys=None, logger=None):
        self._session = session
        self._logger = logger
        self._keys = keys or KeySpace()
        self._guard = threading.Lock()
        self._closed = False
        self._accepted = self._published = self._failed = self._rejected = 0
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(
                name="fetch-receipt-io",
                handler_factory=lambda: self._publish,
                mailbox_capacity=capacity,
                supervision_profile="external_io",
            )
        )

    def submit(self, message) -> AdmissionResult:
        command = PublishFetchReceipt(message.sender, message.message_id)
        with self._guard:
            result = (
                AdmissionResult.CLOSED
                if self._closed
                else self._runtime.tell(self._handle, command)
            )
            if result is AdmissionResult.ACCEPTED:
                self._accepted += 1
            else:
                self._rejected += 1
            return result

    def _publish(self, command):
        if not isinstance(command, PublishFetchReceipt):
            raise TypeError("unsupported fetch receipt command")
        try:
            self._session.put(
                self._keys.fetch_receipt(command.sender, command.message_id), b"ack"
            )
        except Exception as error:
            with self._guard:
                self._failed += 1
            if self._logger is not None:
                self._logger(
                    "warn", "inbox", "inbox.fetch_receipt.publish_failed",
                    messageId=command.message_id, skipped=0,
                    errorType=type(error).__name__, detail=str(error)[:500],
                )
        else:
            with self._guard:
                self._published += 1

    def projection(self):
        with self._guard:
            return FetchReceiptProjection(
                self._accepted,
                self._published,
                self._failed,
                self._rejected,
                self._closed,
            )

    def close(self, timeout=1.0):
        with self._guard:
            self._closed = True
        return self._runtime.stop(self._handle, timeout)
