"""Explicit, default-off loopback pairing server for the S1 protocol.

Construction validates and loads the injected material but never binds or
listens; :meth:`PairingServer.start` is the only listener entry point.  The
caller must supply the node id, host TLS material paths, host derive key,
pairing authority, clock and local loopback port.  There is no CLI, host.py,
GUI, expose integration, approval/reject wire route or production listener in
this module.
"""

from __future__ import annotations

import ipaddress
import math
import socket
import ssl
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec

from ...models import MobileAccessError, _identifier
from ..authority import PairingAuthority
from ..crypto import CryptoError, public_key_raw
from ..protocol import PairingError, encode_frame
from ..state import HostPairingState
from .limits import (
    DEFAULT_FRAME_RATE_LIMIT,
    DEFAULT_FRAME_RATE_WINDOW_SECONDS,
    DEFAULT_HANDSHAKE_TIMEOUT_SECONDS,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_MAX_CONNECTIONS,
    FrameRateLimiter,
    drain_bounded,
    read_frame,
)
from .tls import (
    HANDSHAKE_FAILED,
    HostTlsMaterial,
    load_host_tls_material,
    server_ssl_context,
)

_AUDIT_OPENED = "pairing-channel-opened"
_AUDIT_CLOSED = "pairing-channel-closed"
_AUDIT_TLS_FAILED = "pairing-tls-failed"
_AUDIT_CONNECTION_LIMITED = "pairing-connection-limited"
_AUDIT_RATE_LIMITED = "pairing-frame-rate-limited"


def _loopback_host(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("host must be an IPv4 loopback address")
    if value == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError("host must be an IPv4 loopback address") from error
    if address.version != 4 or not address.is_loopback:
        raise ValueError("host must be an IPv4 loopback address")
    return str(address)


def _port(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 65_535:
        raise ValueError("port must be an integer between 0 and 65535")
    return value


def _positive_seconds(value: object, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field} must be a positive finite number")
    return float(value)


def _connection_limit(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("max_connections must be a positive integer")
    return value


def _node_identifier(value: object) -> str:
    try:
        return _identifier(value, "nodeId")
    except MobileAccessError as error:
        raise ValueError(
            "node_id must be a non-empty protocol identifier"
        ) from error


def _derive_key(value: object) -> ec.EllipticCurvePrivateKey:
    if not isinstance(value, ec.EllipticCurvePrivateKey):
        raise ValueError("host_derive_key must be a P-256 private key")
    try:
        public_key_raw(value.public_key())
    except (AttributeError, CryptoError, ValueError) as error:
        raise ValueError(
            "host_derive_key must be a P-256 private key"
        ) from error
    return value


def _close_quietly(connection: object) -> None:
    close = getattr(connection, "close", None)
    if not callable(close):
        return
    try:
        close()
    except OSError:
        pass


class PairingServer:
    """One explicit loopback TLS listener over the S1 pairing state machine."""

    def __init__(
        self,
        *,
        node_id: str,
        authority: PairingAuthority,
        certfile: str | Path,
        keyfile: str | Path,
        host_derive_key: ec.EllipticCurvePrivateKey,
        audit: Callable[[dict[str, Any]], None],
        host: str = "127.0.0.1",
        port: int = 0,
        clock: Callable[[], float] | None = None,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT_SECONDS,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
        frame_rate_limit: int = DEFAULT_FRAME_RATE_LIMIT,
        frame_rate_window: float = DEFAULT_FRAME_RATE_WINDOW_SECONDS,
    ) -> None:
        self._node_id = _node_identifier(node_id)
        if not callable(audit):
            raise TypeError("audit must be callable")
        if not callable(getattr(authority, "redeem_pairing_code", None)):
            raise TypeError("authority must provide redeem_pairing_code")
        if not callable(getattr(authority, "pairing_status", None)):
            raise TypeError("authority must provide pairing_status")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._authority = authority
        self._audit_sink = audit
        self._clock = clock or time.time
        self._host = _loopback_host(host)
        self._port = _port(port)
        self._handshake_timeout = _positive_seconds(
            handshake_timeout, "handshake_timeout"
        )
        self._idle_timeout = _positive_seconds(idle_timeout, "idle_timeout")
        self._max_connections = _connection_limit(max_connections)
        self._rate = FrameRateLimiter(
            limit=frame_rate_limit, window_seconds=frame_rate_window
        )
        self._derive_key = _derive_key(host_derive_key)
        self._material: HostTlsMaterial = load_host_tls_material(
            certfile, keyfile
        )
        self._tls_context: ssl.SSLContext = server_ssl_context(self._material)

        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._started = False
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._workers: set[threading.Thread] = set()
        self._connections: set[socket.socket] = set()
        self._semaphore = threading.BoundedSemaphore(self._max_connections)

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def host_key_fingerprint(self) -> str:
        """The exact SPKI pin clients must verify before sending a hello."""

        return self._material.fingerprint

    @property
    def started(self) -> bool:
        with self._lock:
            return self._started

    @property
    def bound_port(self) -> int:
        with self._lock:
            if not self._started or self._listener is None:
                raise RuntimeError("pairing server is not started")
            return self._listener.getsockname()[1]

    def start(self) -> None:
        """Bind the injected loopback port and begin accepting connections."""

        with self._lock:
            if self._closed.is_set():
                raise RuntimeError("pairing server is closed")
            if self._started:
                raise RuntimeError("pairing server is already started")
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((self._host, self._port))
                listener.listen(16)
                listener.settimeout(0.2)
            except OSError:
                listener.close()
                raise
            thread = threading.Thread(
                target=self._accept_loop,
                name="m11-pairing-accept",
                daemon=True,
            )
            self._listener = listener
            self._started = True
            self._accept_thread = thread
            try:
                thread.start()
            except RuntimeError:
                self._listener = None
                self._started = False
                self._accept_thread = None
                listener.close()
                raise

    def close(self) -> None:
        """Stop accepting, close open channels and join bounded workers."""

        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
            listener = self._listener
            self._listener = None
            self._started = False
            accept_thread = self._accept_thread
            self._accept_thread = None
            connections = list(self._connections)
            workers = list(self._workers)
        if listener is not None:
            _close_quietly(listener)
        for connection in connections:
            _close_quietly(connection)
        if accept_thread is not None and accept_thread is not (
            threading.current_thread()
        ):
            accept_thread.join(timeout=2.0)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=2.0)
        with self._lock:
            self._workers.clear()
            self._connections.clear()

    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                raw, _address = listener.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                if self._closed.is_set():
                    return
                time.sleep(0.01)
                continue
            if self._closed.is_set():
                _close_quietly(raw)
                return
            if not self._semaphore.acquire(blocking=False):
                self._emit(
                    _AUDIT_CONNECTION_LIMITED,
                    error_code="pairing-connection-limit",
                )
                _close_quietly(raw)
                continue
            with self._lock:
                if self._closed.is_set():
                    self._semaphore.release()
                    _close_quietly(raw)
                    return
                worker = threading.Thread(
                    target=self._serve,
                    args=(raw,),
                    name="m11-pairing-channel",
                    daemon=True,
                )
                self._connections.add(raw)
                self._workers.add(worker)
            try:
                worker.start()
            except RuntimeError:
                with self._lock:
                    self._workers.discard(worker)
                    self._connections.discard(raw)
                self._semaphore.release()
                _close_quietly(raw)

    def _serve(self, raw: socket.socket) -> None:
        conn: ssl.SSLSocket | None = None
        opened = False
        request_id: str | None = None
        error_code: str | None = None
        try:
            try:
                raw.settimeout(self._handshake_timeout)
                conn = self._tls_context.wrap_socket(raw, server_side=True)
            except (ssl.SSLError, OSError, TimeoutError):
                self._emit(
                    _AUDIT_TLS_FAILED, error_code=HANDSHAKE_FAILED
                )
                return
            opened = True
            self._emit(_AUDIT_OPENED)
            try:
                request_id = self._session(conn)
            except PairingError as error:
                error_code = error.code
                if error.code == "pairing-rate-limited":
                    self._emit(
                        _AUDIT_RATE_LIMITED, error_code=error.code
                    )
                self._send_error(conn, error)
                drain_bounded(conn)
            except TimeoutError:
                error_code = "pairing-idle-timeout"
            except EOFError:
                error_code = None
            except (ssl.SSLError, OSError):
                error_code = (
                    None
                    if self._closed.is_set()
                    else "pairing-transport-failed"
                )
            except Exception:
                error_code = "pairing-internal"
                self._send_error(
                    conn,
                    PairingError("pairing-internal", "pairing failed"),
                )
                drain_bounded(conn)
        finally:
            if conn is not None:
                _close_quietly(conn)
            else:
                _close_quietly(raw)
            if opened:
                self._emit(
                    _AUDIT_CLOSED,
                    request_id=request_id,
                    error_code=error_code,
                )
            self._release(raw)

    def _session(self, conn: ssl.SSLSocket) -> str | None:
        host = self._host_state()
        hello = read_frame(conn, timeout=self._idle_timeout)
        self._charge_frame()
        if hello.get("type") != "pairing.hello":
            raise PairingError(
                "pairing-frame-invalid", "pairing frame is invalid"
            )
        challenge = host.challenge(hello)
        conn.sendall(encode_frame(challenge))

        operation = read_frame(conn, timeout=self._idle_timeout)
        self._charge_frame()
        operation_type = operation.get("type")
        if operation_type == "pairing.redeem":
            response = host.redeem(hello, challenge, operation)
        elif operation_type == "pairing.status":
            response = host.status(challenge, operation)
        else:
            raise PairingError(
                "pairing-frame-invalid", "pairing frame is invalid"
            )
        conn.sendall(encode_frame(response))
        request_id = response.get("requestId")
        return request_id if isinstance(request_id, str) else None

    def _host_state(self) -> HostPairingState:
        return HostPairingState(
            node_id=self._node_id,
            host_key_fingerprint=self._material.fingerprint,
            host_signing_key=self._material.private_key,
            host_derive_key=self._derive_key,
            authority=self._authority,
            clock=self._clock,
        )

    def _charge_frame(self) -> None:
        now = self._clock()
        if self._rate.allow(now):
            return
        raise PairingError(
            "pairing-rate-limited",
            "pairing is rate limited",
            retry_after_seconds=self._rate.retry_after_seconds(now),
        )

    @staticmethod
    def _send_error(conn: ssl.SSLSocket, error: PairingError) -> None:
        try:
            conn.sendall(encode_frame(error.as_frame()))
        except (OSError, PairingError):
            pass

    def _emit(
        self,
        event: str,
        *,
        request_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "event": event,
            "nodeId": self._node_id,
            "at": int(self._clock()),
        }
        if request_id is not None:
            payload["requestId"] = request_id
        if error_code is not None:
            payload["errorCode"] = error_code
        try:
            self._audit_sink(payload)
        except Exception:
            # An audit sink failure must not become a remote channel oracle.
            return

    def _release(self, raw: socket.socket) -> None:
        with self._lock:
            self._connections.discard(raw)
            self._workers.discard(threading.current_thread())
        self._semaphore.release()


__all__ = ["PairingServer"]
