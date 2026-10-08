"""An outbound SDK instance is private to one bounded native I/O owner."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from uuid import uuid4

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult


@dataclass(frozen=True, slots=True)
class SendOwnerDm:
    open_id: str
    text: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class SendChat:
    chat_id: str
    text: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class SendChatFile:
    chat_id: str
    name: str
    data: bytes
    media_type: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class SendChatImage:
    chat_id: str
    name: str
    data: bytes
    media_type: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ReplyToMessage:
    message_id: str
    text: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class GatewayCommand:
    operation_id: str
    payload: SendOwnerDm | SendChat | SendChatFile | SendChatImage | ReplyToMessage


class GatewayAdmissionRefused(RuntimeError):
    """The gateway proved that native I/O was not admitted."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Lark outbound gateway {reason}")
        self.reason = reason


@dataclass
class _Reply:
    ready: threading.Event = field(default_factory=threading.Event)
    value: object = None
    error: BaseException | None = None


class GatewayIoAuthority:
    def __init__(self, gateway, *, capacity=32, timeout=65.0, dependents=()):
        self._gateway = gateway
        self._dependents = tuple(dependents)
        self._capacity = capacity
        self._timeout = timeout
        self._guard = threading.Lock()
        self._closed = False
        self._drained = threading.Event()
        self._closer: threading.Thread | None = None
        self._close_error: str | None = None
        self._pending = {}
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(
                name="lark-outbound-gateway",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
                supervision_profile="external_io",
                undelivered_sink=self._undelivered,
            )
        )

    def _call(self, payload, *, await_settlement=False):
        command = GatewayCommand(uuid4().hex, payload)
        reply = _Reply()
        with self._guard:
            if self._closed:
                raise GatewayAdmissionRefused("closed")
            if len(self._pending) >= self._capacity:
                raise GatewayAdmissionRefused("overloaded")
            self._pending[command.operation_id] = reply
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                self._pending.pop(command.operation_id)
                raise GatewayAdmissionRefused(admission.value)
        if not reply.ready.wait(None if await_settlement else self._timeout):
            raise TimeoutError(
                f"gateway request {command.operation_id} remains accepted"
            )
        if reply.error is not None:
            raise reply.error
        return reply.value

    def _undelivered(self, command, reason):
        if not isinstance(command, GatewayCommand):
            return
        with self._guard:
            reply = self._pending.pop(command.operation_id, None)
        if reply is not None:
            reply.error = RuntimeError(reason)
            reply.ready.set()

    def _receive(self, command):
        if not isinstance(command, GatewayCommand):
            raise TypeError("unsupported gateway command")
        with self._guard:
            reply = self._pending[command.operation_id]
        payload = command.payload
        try:
            if isinstance(payload, SendOwnerDm):
                reply.value = self._gateway.send_owner_dm(
                    payload.open_id,
                    payload.text,
                    idempotency_key=payload.idempotency_key,
                )
            elif isinstance(payload, SendChat):
                reply.value = self._gateway.send_chat(
                    payload.chat_id,
                    payload.text,
                    idempotency_key=payload.idempotency_key,
                )
            elif isinstance(payload, SendChatFile):
                reply.value = self._gateway.send_chat_file(
                    payload.chat_id,
                    payload.name,
                    payload.data,
                    media_type=payload.media_type,
                    idempotency_key=payload.idempotency_key,
                )
            elif isinstance(payload, SendChatImage):
                reply.value = self._gateway.send_chat_image(
                    payload.chat_id,
                    payload.name,
                    payload.data,
                    media_type=payload.media_type,
                    idempotency_key=payload.idempotency_key,
                )
            elif isinstance(payload, ReplyToMessage):
                reply.value = self._gateway.reply(
                    payload.message_id,
                    payload.text,
                    idempotency_key=payload.idempotency_key,
                )
            else:
                raise TypeError("unsupported outbound SDK operation")
        except BaseException as error:
            reply.error = error  # original SDK errors stay in local native custody
        finally:
            with self._guard:
                self._pending.pop(command.operation_id, None)
            reply.ready.set()

    def send_owner_dm(self, open_id, text, *, idempotency_key):
        return self._call(SendOwnerDm(open_id, text, idempotency_key))

    def send_owner_dm_settled(self, open_id, text, *, idempotency_key):
        """Receipt-custody port for the isolated user-delivery effect lane."""
        return self._call(
            SendOwnerDm(open_id, text, idempotency_key), await_settlement=True
        )

    def send_chat(self, chat_id, text, *, idempotency_key):
        return self._call(SendChat(chat_id, text, idempotency_key))

    def send_chat_file(self, chat_id, name, data, *, media_type, idempotency_key):
        return self._call(
            SendChatFile(chat_id, name, bytes(data), media_type, idempotency_key)
        )

    def send_chat_image(self, chat_id, name, data, *, media_type, idempotency_key):
        return self._call(
            SendChatImage(chat_id, name, bytes(data), media_type, idempotency_key)
        )

    def reply(self, message_id, text, *, idempotency_key):
        return self._call(ReplyToMessage(message_id, text, idempotency_key))

    def close(self, timeout=2.0):
        with self._guard:
            self._closed = True
            if self._closer is None:
                # A rejected fresh gateway may have no caller left to retry a
                # zero-budget close. This one retained task owns the complete
                # dependency chain until it drains, even after caller timeout.
                self._closer = threading.Thread(
                    target=self._drain_close, name="lark-gateway-close", daemon=True
                )
                self._closer.start()
        return self._drained.wait(max(0.0, timeout))

    def _drain_close(self):
        import time

        def drain(close):
            while True:
                try:
                    if close(0.05):
                        with self._guard:
                            self._close_error = None
                        return
                except Exception as error:
                    # A failed close does not revoke resource custody. Retain
                    # a diagnostic and retry this same owner, never skip it.
                    with self._guard:
                        self._close_error = type(error).__name__
                time.sleep(0.01)

        drain(lambda timeout: self._runtime.stop(self._handle, timeout))
        for owner in self._dependents:
            drain(owner.close)
        self._drained.set()
