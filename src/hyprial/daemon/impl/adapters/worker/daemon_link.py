"""Private authenticated socket link shared by daemon and worker endpoints."""

from __future__ import annotations

import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from .control import AdapterControlIoOwner
from .wire import (
    CONTROL_SCHEMA,
    WorkerIdentity,
    WorkerWireError,
    build_control_frame,
    recv_worker_frame,
    require_worker_identity,
    send_worker_frame,
)

if TYPE_CHECKING:
    from hyprial.kernel import ActorHost


class WorkerDisconnected(ConnectionError):
    pass


@dataclass(slots=True)
class _Pending:
    ready: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: BaseException | None = None


class AdapterDaemonLink:
    """One private socket, authenticated frames, and correlated receipts."""

    def __init__(
        self,
        control_socket: socket.socket,
        token: bytes,
        identity: WorkerIdentity,
        *,
        callback_handler=None,
        request_handler=None,
        closed_callback=None,
        callback_capacity: int = 32,
        actor_host: ActorHost | None = None,
    ) -> None:
        self._socket = control_socket
        self._token = bytes(token)
        self.identity = identity
        self._request_handler = request_handler
        self._closed_callback = closed_callback
        self._send_lock = threading.Lock()
        self._guard = threading.Lock()
        self._closed = False
        self._pending: dict[str, _Pending] = {}
        self._requests = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"adapter-request-{identity.instance_id}-{identity.generation}",
        )
        self._callbacks = AdapterControlIoOwner(
            callback_handler or (lambda _payload: None),
            capacity=callback_capacity,
            name=f"adapter-callback-{identity.instance_id}-{identity.generation}",
            actor_host=actor_host,
        )
        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"adapter-link-{identity.instance_id}-{identity.generation}",
            daemon=True,
        )
        self._reader.start()

    @property
    def closed(self) -> bool:
        with self._guard:
            return self._closed

    def request(self, control_type: str, payload: object, timeout: float) -> object:
        operation_id = uuid4().hex
        pending = _Pending()
        with self._guard:
            if self._closed:
                raise WorkerDisconnected("adapter worker link is closed")
            self._pending[operation_id] = pending
        try:
            self._send(
                build_control_frame(
                    self.identity, operation_id, control_type, payload
                )
            )
        except BaseException:
            with self._guard:
                self._pending.pop(operation_id, None)
            raise
        if not pending.ready.wait(max(0.0, timeout)):
            with self._guard:
                self._pending.pop(operation_id, None)
            self._break(WorkerDisconnected("adapter worker request timed out"))
            raise WorkerDisconnected("adapter worker request timed out")
        if pending.error is not None:
            raise pending.error
        return pending.result

    def emit_callback(self, payload: object) -> None:
        self._send(build_control_frame(self.identity, uuid4().hex, "callback", payload))

    def wait_callbacks(self, timeout: float) -> bool:
        return self._callbacks.drain(timeout)

    def close_admission(self) -> None:
        self._callbacks.close_admission()

    def _send(self, frame: dict[str, object]) -> None:
        try:
            with self._send_lock:
                send_worker_frame(self._socket, frame, self._token)
        except (OSError, WorkerWireError) as error:
            failure = WorkerDisconnected(str(error))
            self._break(failure)
            raise failure from error

    def _read_loop(self) -> None:
        try:
            while True:
                frame = recv_worker_frame(self._socket, self._token)
                require_worker_identity(frame, self.identity)
                fields = frame.as_dict()
                if fields.get("schema") != CONTROL_SCHEMA:
                    raise WorkerWireError(
                        "ADAPTER_WORKER_FRAME_INVALID", "control socket received non-control frame"
                    )
                control_type = fields.get("type")
                if control_type == "result":
                    self._receive_result(fields)
                elif control_type == "callback":
                    if not self._callbacks.submit(fields.get("payload")):
                        raise WorkerDisconnected("adapter callback lane is closed or overloaded")
                elif self._request_handler is not None:
                    self._requests.submit(self._receive_request, fields)
                else:
                    raise WorkerWireError(
                        "ADAPTER_WORKER_FRAME_INVALID", f"unexpected control type: {control_type}"
                    )
        except (OSError, WorkerWireError, WorkerDisconnected) as error:
            self._break(WorkerDisconnected(str(error)))

    def _receive_result(self, fields: dict[str, object]) -> None:
        operation_id = fields.get("operationId")
        payload = fields.get("payload")
        if not isinstance(operation_id, str) or not isinstance(payload, dict):
            raise WorkerWireError("ADAPTER_WORKER_FRAME_INVALID", "invalid result frame")
        with self._guard:
            pending = self._pending.pop(operation_id, None)
        if pending is None:
            return
        if payload.get("ok") is True:
            pending.result = payload.get("result")
        else:
            pending.error = WorkerDisconnected(str(payload.get("error") or "worker refused"))
        pending.ready.set()

    def _receive_request(self, fields: dict[str, object]) -> None:
        operation_id = fields.get("operationId")
        control_type = fields.get("type")
        if not isinstance(operation_id, str) or not isinstance(control_type, str):
            raise WorkerWireError("ADAPTER_WORKER_FRAME_INVALID", "invalid control request")
        try:
            result = self._request_handler(control_type, fields.get("payload"))
            payload = {"ok": True, "result": result}
        except Exception as error:
            payload = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        self._send(build_control_frame(self.identity, operation_id, "result", payload))
        if control_type == "close":
            self._notify_closed()

    def _notify_closed(self) -> None:
        with self._guard:
            callback = self._closed_callback
            self._closed_callback = None
        if callback is not None:
            callback()

    def _break(self, error: BaseException) -> None:
        with self._guard:
            if self._closed:
                return
            self._closed = True
            callback = self._closed_callback
            self._closed_callback = None
            pending = tuple(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.error = error
            item.ready.set()
        self._callbacks.close_admission()
        self._requests.shutdown(wait=False, cancel_futures=True)
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._socket.close()
        except OSError:
            pass
        if callback is not None:
            callback()

    def close(self, timeout: float = 2.0) -> bool:
        self._break(WorkerDisconnected("adapter worker link closed"))
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=max(0.0, timeout))
        callbacks_closed = self._callbacks.close(timeout)
        return callbacks_closed and not self._reader.is_alive()


__all__ = ["AdapterDaemonLink", "WorkerDisconnected"]
