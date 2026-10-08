"""Actor ownership of epoch-scoped dispatch classification facts."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult


@dataclass(frozen=True, slots=True)
class OpenConversation:
    operation_id: str
    conversation_id: str
    send_operation_id: str


@dataclass(frozen=True, slots=True)
class SendClassified:
    operation_id: str
    dispatch: bool


@dataclass(frozen=True, slots=True)
class DispatchDiagnosticProjection:
    without_pac: int
    conversation: int
    known_conversations: int
    overloaded: int


@dataclass(slots=True)
class _Reply:
    ready: threading.Event = field(default_factory=threading.Event)
    value: bool | None = None


class DispatchDiagnostics:
    """One mailbox serializes opener decisions and diagnostic counters."""

    def __init__(self, *, capacity: int = 128, runtime: ActorRuntime | None = None):
        self._runtime = runtime or ActorRuntime()
        self._guard = threading.Lock()
        self._pending: dict[str, _Reply] = {}
        self._conversations: dict[str, str] = {}
        self._without_pac = self._conversation = self._overloaded = 0
        self._closed = False
        self._capacity = capacity
        self._handle = self._runtime.start(
            ActorSpec(
                name="dispatch-diagnostics",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
            )
        )

    def open_conversation(
        self, conversation_id: str, send_operation_id: str, *, timeout: float = 2.0
    ) -> bool:
        return self._call(
            OpenConversation(uuid.uuid4().hex, conversation_id, send_operation_id),
            timeout,
        )

    def classify(self, dispatch: bool, *, timeout: float = 2.0) -> None:
        self._call(SendClassified(uuid.uuid4().hex, dispatch), timeout)

    def _call(self, command: OpenConversation | SendClassified, timeout: float) -> bool:
        reply = _Reply()
        with self._guard:
            if self._closed or len(self._pending) >= self._capacity:
                self._overloaded += 1
                raise TimeoutError("dispatch diagnostic authority overloaded")
            self._pending[command.operation_id] = reply
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                self._pending.pop(command.operation_id)
                self._overloaded += 1
                raise TimeoutError(f"dispatch diagnostic admission {admission.value}")
        if not reply.ready.wait(max(0.0, timeout)):
            # Accepted command continues; timeout never cancels its effect.
            raise TimeoutError("dispatch diagnostic command remains accepted")
        assert reply.value is not None
        return reply.value

    def _receive(self, command: object) -> None:
        with self._guard:
            if isinstance(command, OpenConversation):
                incumbent = self._conversations.get(command.conversation_id)
                value = incumbent is None or incumbent == command.send_operation_id
                if incumbent is None:
                    self._conversations[command.conversation_id] = command.send_operation_id
            elif isinstance(command, SendClassified):
                if command.dispatch:
                    self._without_pac += 1
                else:
                    self._conversation += 1
                value = True
            else:
                raise TypeError("unsupported dispatch diagnostic command")
            reply = self._pending.pop(command.operation_id, None)
            if reply is not None:
                reply.value = value
                reply.ready.set()

    def projection(self) -> DispatchDiagnosticProjection:
        with self._guard:
            return DispatchDiagnosticProjection(
                self._without_pac, self._conversation,
                len(self._conversations), self._overloaded,
            )

    def close(self, timeout: float = 5.0) -> bool:
        with self._guard:
            self._closed = True
        return self._runtime.stop(self._handle, timeout)
