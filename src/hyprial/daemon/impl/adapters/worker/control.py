"""Bounded single-actor lane for native callbacks."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from hyprial.daemon.impl.adapters.contracts import canonical_json_v1, freeze_json_v1
from hyprial.daemon.impl.actor_custody import ActorHostCustody
from hyprial.kernel import ActorSpec, AdmissionResult

if TYPE_CHECKING:
    from hyprial.kernel import ActorHost


@dataclass(frozen=True, slots=True)
class CallbackDelivery:
    payload_bytes: bytes


class AdapterControlIoOwner:
    """Marshal arbitrary callback threads onto one bounded actor mailbox."""

    def __init__(
        self,
        handler: Callable[[object], object],
        *,
        capacity: int = 32,
        name: str = "adapter-worker-callbacks",
        actor_host: ActorHost | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._handler = handler
        self._guard = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._pending = 0
        self._closed = False
        self._hosts = ActorHostCustody(actor_host, owner="adapter-worker-control")
        self._cell = self._hosts.host.spawn(
            ActorSpec(
                name=name,
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
                supervision_profile="external_io",
            )
        )

    def submit(self, payload: object) -> bool:
        copied = bytes(canonical_json_v1(freeze_json_v1(payload)))
        delivery = CallbackDelivery(copied)
        with self._guard:
            if self._closed:
                return False
            self._pending += 1
            self._idle.clear()
            admitted = self._cell.tell(delivery)
            if admitted is not AdmissionResult.ACCEPTED:
                self._pending -= 1
                if self._pending == 0:
                    self._idle.set()
                return False
        return True

    def _receive(self, command: object) -> None:
        if not isinstance(command, CallbackDelivery):
            raise TypeError("unsupported callback command")
        try:
            payload: Any = json.loads(command.payload_bytes)
            self._handler(payload)
        finally:
            with self._guard:
                self._pending -= 1
                if self._pending == 0:
                    self._idle.set()

    def close_admission(self) -> None:
        with self._guard:
            self._closed = True

    def drain(self, timeout: float) -> bool:
        return self._idle.wait(max(0.0, timeout))

    def close(self, timeout: float = 2.0) -> bool:
        self.close_admission()
        drained = self.drain(timeout)
        stopped = self._cell.drain(timeout).complete
        self._hosts.shutdown(timeout)
        return drained and stopped


__all__ = ["AdapterControlIoOwner", "CallbackDelivery"]
