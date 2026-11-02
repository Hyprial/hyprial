"""One app-server WebSocket client: id-matched responses, dispatched notifications.

Decision record: notifications are protocol facts that arrive interleaved with
responses (a TUI turn can start while a request is in flight), so a wait for a
response must not swallow them. A single reader thread owns frame decoding;
callers only ever send. Listeners must return quickly and must never issue a
request on the same connection from the reader thread (that would wait on a
frame the reader itself has to decode).

Decision record (rel0553 codex-interactive fix, 2026-11-02): after the carrier
joins the TUI thread with ``thread/resume``, the app-server also delivers its
consent surfaces -- ``mcpServer/elicitation/request`` and the ``*approval*``
requests -- to every joined client connection. The daemon connection is an
observer, never the approver: answering one is a denial the user never made
(codex then records "user rejected MCP tool call" and the TUI turn never runs
the tool, so the reply never reaches the daemon -- loopback regression after
#2242). Consent requests are therefore left unanswered for the user's TUI
client to settle; every other server request keeps the PR1 ``-32601`` refusal.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path

from hyprial.daemon.impl.harnesses.codex.process import _notification_turn_id

_READER_JOIN_SECONDS = 5.0

NotificationListener = Callable[[dict[str, object]], None]


def _is_consent_request(message: dict[str, object]) -> bool:
    """True for the app-server's consent surfaces the TUI must settle."""

    method = message.get("method")
    if not isinstance(method, str):
        return False
    lowered = method.lower()
    if "elicitation" in lowered or "approval" in lowered:
        return True
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    return isinstance(meta, dict) and "codex_approval_kind" in meta


class _UnixWebSocket:
    """Minimal Unix-domain RFC 6455 client for Codex's local app-server."""

    def __init__(self, path: Path, *, timeout: float) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall(
            (
                "GET / HTTP/1.1\r\nHost: localhost\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        response = self._read_until(b"\r\n\r\n")
        if b"101" not in response.split(b"\r\n", 1)[0]:
            raise ConnectionError("Codex app-server WebSocket handshake failed")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        )
        if expected not in response:
            raise ConnectionError("Codex app-server WebSocket accept mismatch")
        self.sock.settimeout(None)
        self._send_lock = threading.Lock()

    def send_json(self, value: dict[str, object]) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode()
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        length = len(masked)
        if length < 126:
            header = bytes([0x81, 0x80 | length])
        elif length < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", length)
        with self._send_lock:
            self.sock.sendall(header + mask + masked)

    def recv_json(self) -> dict[str, object]:
        while True:
            first, second = self._recv_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if second & 0x80 else b""
            payload = bytearray(self._recv_exact(length))
            if mask:
                for index in range(length):
                    payload[index] ^= mask[index % 4]
            if opcode == 0x9:
                continue
            if opcode == 0x8:
                raise ConnectionError("Codex app-server WebSocket closed")
            if opcode == 0x1:
                value = json.loads(payload.decode())
                if not isinstance(value, dict):
                    raise ConnectionError("Codex app-server returned non-object JSON")
                return value

    def close(self) -> None:
        try:
            self.sock.sendall(bytes([0x88, 0x80, 0, 0, 0, 0]))
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    def _read_until(self, marker: bytes) -> bytes:
        data = bytearray()
        while marker not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Codex app-server closed during handshake")
            data.extend(chunk)
        return bytes(data)

    def _recv_exact(self, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise ConnectionError("Codex app-server WebSocket closed unexpectedly")
            data.extend(chunk)
        return bytes(data)


class CodexRpcConnection:
    """The app-server connection: one reader, id-matched responses, listeners."""

    def __init__(self, socket_path: Path, *, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self._socket = _UnixWebSocket(Path(socket_path), timeout=timeout)
        self._responses: dict[int, dict[str, object]] = {}
        self._response_cv = threading.Condition()
        self._listeners: tuple[NotificationListener, ...] = ()
        self._listener_lock = threading.Lock()
        self._closed: BaseException | None = None
        self._turn_activity: dict[tuple[str, str], int] = {}
        self._activity_sequence = 0
        self._reader = threading.Thread(
            target=self._read_loop,
            name="hyprial-codex-app-server-reader",
            daemon=True,
        )
        self._reader.start()

    @property
    def closed(self) -> bool:
        return self._closed is not None

    def request(
        self, method: str, params: object, request_id: int
    ) -> dict[str, object]:
        self._socket.send_json(
            {"method": method, "id": request_id, "params": params}
        )
        deadline = time.monotonic() + self.timeout
        with self._response_cv:
            while request_id not in self._responses:
                closed = self._closed
                if closed is not None:
                    raise ConnectionError(
                        "Codex app-server connection was closed"
                    ) from closed
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Codex RPC {method} timed out")
                self._response_cv.wait(min(remaining, 0.25))
            return self._responses.pop(request_id)

    def send_notification(self, method: str, params: object) -> None:
        self._socket.send_json({"method": method, "params": params})

    def add_notification_listener(
        self, listener: NotificationListener
    ) -> Callable[[], None]:
        with self._listener_lock:
            self._listeners = (*self._listeners, listener)
        if self.closed:
            listener({"method": "_hyprial/connection-lost"})

        def remove() -> None:
            with self._listener_lock:
                self._listeners = tuple(
                    item for item in self._listeners if item is not listener
                )

        return remove

    def close(self) -> None:
        self._fail(ConnectionError("Codex app-server connection closed by hyprial"))
        self._socket.close()
        self._reader.join(timeout=_READER_JOIN_SECONDS)

    def _fail(self, error: BaseException) -> None:
        with self._response_cv:
            first = self._closed is None
            if first:
                self._closed = error
            self._response_cv.notify_all()
        if first:
            self._route({"method": "_hyprial/connection-lost"})

    def _read_loop(self) -> None:
        try:
            while True:
                message = self._socket.recv_json()
                self._route(message)
        except BaseException as error:  # noqa: BLE001 - transport failure boundary
            self._fail(
                error
                if isinstance(error, ConnectionError)
                else ConnectionError(f"Codex app-server connection failed: {error}")
            )

    def _route(self, message: dict[str, object]) -> None:
        identifier = message.get("id")
        if "method" in message and identifier is not None:
            if _is_consent_request(message):
                return
            self._answer_server_request(identifier)
            return
        if "method" in message:
            self._observe_turn_activity(message)
            with self._listener_lock:
                listeners = self._listeners
            for listener in listeners:
                try:
                    listener(message)
                except Exception:  # noqa: BLE001 - a listener cannot break the wire
                    continue
            return
        if isinstance(identifier, int):
            with self._response_cv:
                self._responses[identifier] = message
                self._response_cv.notify_all()

    def _observe_turn_activity(self, message: dict[str, object]) -> None:
        method = message.get("method")
        params = message.get("params")
        if (
            "id" in message or not isinstance(method, str)
            or method == "thread/status/changed" or not isinstance(params, dict)
        ):
            return
        thread_id = params.get("threadId")
        turn_id = _notification_turn_id(params)
        if not isinstance(thread_id, str) or turn_id is None:
            return
        sequence = self._activity_sequence + 1
        self._activity_sequence = sequence
        key = (thread_id, turn_id)
        self._turn_activity.pop(key, None)
        self._turn_activity[key] = sequence
        if len(self._turn_activity) > 128:
            self._turn_activity.pop(next(iter(self._turn_activity)))

    def turn_activity_sequence(self, thread_id: str, turn_id: str) -> int:
        return self._turn_activity.get((thread_id, turn_id), 0)

    def _answer_server_request(self, identifier: object) -> None:
        self._socket.send_json(
            {
                "id": identifier,
                "error": {
                    "code": -32601,
                    "message": "hyprial PR1 does not handle app-server server requests",
                },
            }
        )


def open_app_server_connection(
    socket_path: Path, *, timeout: float = 15.0
) -> CodexRpcConnection:
    return CodexRpcConnection(socket_path, timeout=timeout)
