"""Bounded per-worker native control exchanges, off caller threads."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from uuid import uuid4

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult


@dataclass(frozen=True, slots=True)
class ControlDelivery:
    operation_id: str
    delivery_id: str
    frame: bytes
    deadline: float


@dataclass
class _Reply:
    ready: threading.Event = field(default_factory=threading.Event)
    accepted: bool = False
    error: BaseException | None = None


class ControlIoOwner:
    def __init__(self, exchange, *, capacity=32, timeout=16.0):
        self._exchange = exchange
        self._capacity = capacity
        self._timeout = timeout
        self._guard = threading.Lock()
        self._pending = {}
        self._closed = False
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(
                name="lark-control-io",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
                supervision_profile="external_io",
            )
        )

    def deliver(self, frame, delivery_id):
        command = ControlDelivery(
            uuid4().hex,
            delivery_id,
            json.dumps(frame, separators=(",", ":")).encode(),
            time.monotonic() + self._timeout,
        )
        reply = _Reply()
        with self._guard:
            if self._closed or len(self._pending) >= self._capacity:
                return False
            self._pending[command.operation_id] = reply
            result = self._runtime.tell(self._handle, command)
            if result is not AdmissionResult.ACCEPTED:
                self._pending.pop(command.operation_id)
                return False
        if not reply.ready.wait(max(0.0, command.deadline - time.monotonic())):
            return False  # no confirmed native receipt; accepted work retains custody
        if reply.error is not None:
            raise reply.error
        return reply.accepted

    def _receive(self, command):
        if not isinstance(command, ControlDelivery):
            raise TypeError("unsupported control command")
        with self._guard:
            reply = self._pending[command.operation_id]
        try:
            if time.monotonic() < command.deadline:
                reply.accepted = bool(
                    self._exchange(json.loads(command.frame), command.delivery_id)
                )
        except BaseException as error:
            reply.error = error
        finally:
            with self._guard:
                self._pending.pop(command.operation_id, None)
            reply.ready.set()

    def close_admission(self):
        with self._guard:
            self._closed = True

    def close(self, timeout=2.0):
        self.close_admission()
        return self._runtime.stop(self._handle, timeout)
