"""One bounded file-I/O owner for durable user-delivery idempotency."""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult

if TYPE_CHECKING:
    from hyprial.daemon.impl.squire.addressing  import UserDeliveryResult


@dataclass(frozen=True, slots=True)
class ReadDelivery:
    key: str


@dataclass(frozen=True, slots=True)
class ReadDeliveryMessage:
    message_id: str


@dataclass(frozen=True, slots=True)
class MarkDeliveryDuplicate:
    key: str


@dataclass(frozen=True, slots=True)
class RecordDelivery:
    key: str
    result: UserDeliveryResult


@dataclass(frozen=True, slots=True)
class LedgerCommand:
    operation_id: str
    payload: ReadDelivery | ReadDeliveryMessage | MarkDeliveryDuplicate | RecordDelivery


@dataclass(frozen=True, slots=True)
class LedgerCommandReceipt:
    operation_id: str
    kind: str
    key: str
    expected: UserDeliveryResult | None = None


class LedgerOutcomeUnknown(TimeoutError):
    """A local ledger command may have committed; recover by this receipt."""

    def __init__(self, receipt: LedgerCommandReceipt) -> None:
        super().__init__(
            f"user delivery ledger outcome is unknown: {receipt.operation_id}"
        )
        self.receipt = receipt


@dataclass
class _Reply:
    ready: threading.Event = field(default_factory=threading.Event)
    result: UserDeliveryResult | None = None
    error: BaseException | None = None


class UserDeliveryLedgerAuthority:
    def __init__(self, store, *, capacity=128, completion_timeout=2.0):
        if completion_timeout <= 0:
            raise ValueError("completion_timeout must be positive")
        self.path = store.path
        self._store = store
        self._capacity = capacity
        self._completion_timeout = completion_timeout
        self._guard = threading.Lock()
        self._pending = {}
        self._completed: OrderedDict[str, _Reply] = OrderedDict()
        self._closed = False
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(
                name="user-delivery-ledger-io",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
                supervision_profile="external_io",
                undelivered_sink=self._undelivered,
            )
        )

    @staticmethod
    def _receipt(operation_id, payload):
        if isinstance(payload, ReadDeliveryMessage):
            return LedgerCommandReceipt(operation_id, "by-message", payload.message_id)
        if isinstance(payload, ReadDelivery):
            return LedgerCommandReceipt(operation_id, "read", payload.key)
        if isinstance(payload, MarkDeliveryDuplicate):
            return LedgerCommandReceipt(operation_id, "mark-duplicate", payload.key)
        return LedgerCommandReceipt(
            operation_id, "record", payload.key, payload.result
        )

    def _call(self, payload):
        command = LedgerCommand(uuid4().hex, payload)
        receipt = self._receipt(command.operation_id, payload)
        reply = _Reply()
        with self._guard:
            if self._closed or len(self._pending) >= self._capacity:
                raise RuntimeError("user delivery ledger closed or overloaded")
            self._pending[command.operation_id] = reply
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                self._pending.pop(command.operation_id)
                raise RuntimeError(f"user ledger admission {admission.value}")
        if not reply.ready.wait(self._completion_timeout):
            raise LedgerOutcomeUnknown(receipt)
        if reply.error is not None:
            raise reply.error
        return reply.result

    def recover(self, receipt):
        if not isinstance(receipt, LedgerCommandReceipt):
            raise TypeError("receipt must be a LedgerCommandReceipt")
        with self._guard:
            reply = self._pending.get(receipt.operation_id)
            if reply is None:
                reply = self._completed.get(receipt.operation_id)
        if reply is not None:
            if not reply.ready.wait(self._completion_timeout):
                raise LedgerOutcomeUnknown(receipt)
            if reply.error is not None:
                raise reply.error
            return reply.result
        try:
            if receipt.kind == "by-message":
                result = self._call(ReadDeliveryMessage(receipt.key))
            else:
                result = self._call(ReadDelivery(receipt.key))
        except LedgerOutcomeUnknown:
            raise LedgerOutcomeUnknown(receipt) from None
        if receipt.kind == "record" and result != receipt.expected:
            raise LedgerOutcomeUnknown(receipt)
        if receipt.kind == "mark-duplicate" and (
            result is None or not result.duplicate
        ):
            raise LedgerOutcomeUnknown(receipt)
        return result

    def _remember_completed_locked(self, operation_id, reply):
        self._completed[operation_id] = reply
        self._completed.move_to_end(operation_id)
        while len(self._completed) > self._capacity:
            self._completed.popitem(last=False)

    def _undelivered(self, command, reason):
        if not isinstance(command, LedgerCommand):
            return
        with self._guard:
            reply = self._pending.pop(command.operation_id, None)
            if reply is not None:
                reply.error = RuntimeError(reason)
                self._remember_completed_locked(command.operation_id, reply)
        if reply is not None:
            reply.ready.set()

    def _receive(self, command):
        if not isinstance(command, LedgerCommand):
            raise TypeError("unsupported user ledger command")
        with self._guard:
            reply = self._pending[command.operation_id]
        payload = command.payload
        try:
            if isinstance(payload, ReadDelivery):
                reply.result = self._store.get(payload.key)
            elif isinstance(payload, ReadDeliveryMessage):
                reply.result = self._store.by_message_id(payload.message_id)
            elif isinstance(payload, MarkDeliveryDuplicate):
                reply.result = self._store.mark_duplicate(payload.key)
            elif isinstance(payload, RecordDelivery):
                reply.result = self._store.record(payload.key, payload.result)
        except BaseException as error:
            reply.error = error
        finally:
            with self._guard:
                self._pending.pop(command.operation_id, None)
                self._remember_completed_locked(command.operation_id, reply)
            reply.ready.set()

    def get(self, key):
        return self._call(ReadDelivery(key))

    def by_message_id(self, message_id):
        return self._call(ReadDeliveryMessage(message_id))

    def mark_duplicate(self, key):
        return self._call(MarkDeliveryDuplicate(key))

    def record(self, key, result):
        return self._call(RecordDelivery(key, result))

    def close(self, timeout=5.0):
        with self._guard:
            self._closed = True
        return self._runtime.stop(self._handle, timeout)
