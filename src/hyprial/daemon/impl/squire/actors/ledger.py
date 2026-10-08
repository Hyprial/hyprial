"""One bounded file-I/O owner for durable user-delivery idempotency."""

from __future__ import annotations

import threading
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


@dataclass
class _Reply:
    ready: threading.Event = field(default_factory=threading.Event)
    result: UserDeliveryResult | None = None
    error: BaseException | None = None


class UserDeliveryLedgerAuthority:
    def __init__(self, store, *, capacity=128):
        self.path = store.path
        self._store = store
        self._capacity = capacity
        self._guard = threading.Lock()
        self._pending = {}
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

    def _call(self, payload):
        command = LedgerCommand(uuid4().hex, payload)
        reply = _Reply()
        with self._guard:
            if self._closed or len(self._pending) >= self._capacity:
                raise RuntimeError("user delivery ledger closed or overloaded")
            self._pending[command.operation_id] = reply
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                self._pending.pop(command.operation_id)
                raise RuntimeError(f"user ledger admission {admission.value}")
        # This port is consumed only by isolated delivery I/O workers. A caller
        # protocol deadline must not turn a still-persisting native success into
        # a permanent negative ledger result.
        reply.ready.wait()
        if reply.error is not None:
            raise reply.error
        return reply.result

    def _undelivered(self, command, reason):
        if not isinstance(command, LedgerCommand):
            return
        with self._guard:
            reply = self._pending.pop(command.operation_id, None)
        if reply is not None:
            reply.error = RuntimeError(reason)
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
