"""An in-memory pipe-like backend that simulates a platform without AF_UNIX.

No socket family is used anywhere: the endpoint file is a JSON marker,
connections travel through an in-process registry keyed by endpoint path,
and the data plane is a pair of capacity-bounded byte channels with socket
timeout semantics (blocking reads, deadline sends, EOF on close).  The
accepted peer carries no OS credentials (``pid``/``uid`` ``None``,
``same_user_enforced`` ``True``) -- exactly the Windows named-pipe contract.
Injecting this backend exercises every wrapper rule (parent directory mode,
stale endpoint cleanup, endpoint hardening, bounded response writes) without
a second real transport.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

from hyprial.kernel.impl.manysys.ipc import (
    LocalIpcListener,
    LocalIpcPeer,
    LocalIpcStream,
)

MEMORY_PIPE_TRANSPORT = "memory-pipe-v1"
MEMORY_PIPE_CAPACITY = 64 * 1024


class _MemoryChannel:
    """One direction of a pipe: bounded bytes with blocking timeout I/O."""

    def __init__(self, capacity: int = MEMORY_PIPE_CAPACITY) -> None:
        self._capacity = capacity
        self._buffer = bytearray()
        self._closed = False
        self._lock = threading.Lock()
        self._data_ready = threading.Condition(self._lock)
        self._space_ready = threading.Condition(self._lock)

    def send(self, data: bytes, timeout: float | None) -> int:
        view = memoryview(data)
        offset = 0
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while offset < len(view):
                if self._closed:
                    raise BrokenPipeError("memory pipe end is closed")
                space = self._capacity - len(self._buffer)
                if space <= 0:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise TimeoutError("memory pipe send timed out")
                    self._space_ready.wait(remaining)
                    continue
                count = min(space, len(view) - offset)
                self._buffer += view[offset : offset + count]
                offset += count
                self._data_ready.notify_all()
        return offset

    def recv(self, size: int, timeout: float | None) -> bytes:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while not self._buffer:
                if self._closed:
                    return b""
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("memory pipe read timed out")
                self._data_ready.wait(remaining)
            chunk = self._buffer[:size]
            del self._buffer[:size]
            self._space_ready.notify_all()
            return bytes(chunk)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._data_ready.notify_all()
            self._space_ready.notify_all()


class _MemoryTransport:
    """Both directions of one connection behind the stream's surface."""

    def __init__(self, incoming: _MemoryChannel, outgoing: _MemoryChannel) -> None:
        self._incoming = incoming
        self._outgoing = outgoing
        self._timeout: float | None = None

    def settimeout(self, timeout: float | None) -> None:
        self._timeout = timeout

    def recv(self, size: int) -> bytes:
        return self._incoming.recv(size, self._timeout)

    def send(self, data: bytes) -> int:
        return self._outgoing.send(data, self._timeout)

    def sendall(self, data: bytes) -> None:
        self._outgoing.send(data, self._timeout)

    def shutdown(self, _how: int) -> None:
        self._outgoing.close()

    def close(self) -> None:
        self._incoming.close()
        self._outgoing.close()


class _MemoryListener:
    """A listener whose accepted transports arrive from a queue."""

    def __init__(
        self,
        forget: Any,
        pending: "queue.SimpleQueue[_MemoryTransport]",
    ) -> None:
        self._forget = forget
        self._pending = pending
        self._timeout: float | None = 0.25
        self._closed = False

    def settimeout(self, timeout: float | None) -> None:
        self._timeout = timeout

    def accept(self) -> tuple[_MemoryTransport, None]:
        if self._closed:
            raise OSError("memory pipe listener is closed")
        if self._timeout is None:
            transport = self._pending.get()
        else:
            try:
                transport = self._pending.get(timeout=self._timeout)
            except queue.Empty:
                raise TimeoutError("memory pipe accept timed out") from None
        return transport, None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        while True:
            try:
                self._pending.get_nowait().close()
            except queue.Empty:
                break
        self._forget()


class MemoryPipeBackend:
    """A :class:`LocalIpcBackend` whose connections are in-process channels."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, queue.SimpleQueue[_MemoryTransport]] = {}

    def _forget(self, key: str) -> None:
        with self._lock:
            self._pending.pop(key, None)

    def listen(
        self, endpoint: Path, *, backlog: int, gui_write_sid: str | None
    ) -> LocalIpcListener:
        key = str(endpoint)
        with self._lock:
            if key in self._pending:
                raise OSError("memory pipe endpoint is already listened")
            pending: queue.SimpleQueue[_MemoryTransport] = queue.SimpleQueue()
            self._pending[key] = pending
        try:
            endpoint.write_text(
                json.dumps({"transport": MEMORY_PIPE_TRANSPORT}), encoding="utf-8"
            )
            os.chmod(endpoint, 0o600)
        except BaseException:
            self._forget(key)
            raise
        return LocalIpcListener(
            _MemoryListener(lambda: self._forget(key), pending),
            peer_factory=lambda _connection: LocalIpcPeer(
                pid=None, uid=None, same_user_enforced=True
            ),
        )

    def connect(
        self, endpoint: Path, timeout: float, *, clock: Any | None = None
    ) -> LocalIpcStream:
        try:
            document = json.loads(endpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise OSError("invalid local IPC endpoint") from error
        if not isinstance(document, dict) or document.get("transport") != MEMORY_PIPE_TRANSPORT:
            raise OSError("invalid local IPC endpoint")
        with self._lock:
            pending = self._pending.get(str(endpoint))
        if pending is None:
            raise ConnectionRefusedError(f"no listener at {endpoint}")
        client_to_server = _MemoryChannel()
        server_to_client = _MemoryChannel()
        server_transport = _MemoryTransport(client_to_server, server_to_client)
        client_transport = _MemoryTransport(server_to_client, client_to_server)
        pending.put(server_transport)
        return LocalIpcStream(client_transport)

    def path_limit(self) -> int | None:
        return None
