# Decision record (manysys/ipc/streams, W1-MANYSYS 2026-11-21):
# - Migrated from kernel/impl/platform/local_ipc/streams.py into the leaf OS
#   package hyprial.kernel.impl.manysys.
# - Transport-neutral stream, listener, and peer types expose only primitive OS
#   identity fields and stdlib I/O methods so a Rust extension can replace this
#   surface 1:1.
"""Transport-neutral local IPC stream, listener and peer types.

Decision record (ipc-seam 2026-11-19): the bounded response write that
``daemon/impl/ipc/server.py`` owned for both stream kinds, and the peer
hangup probe, moved here so every local transport answers the same surface.
A transport that carries its own safe response writer (the Windows named
pipe) keeps it; a socket transport gets the deadline-driven partial-send
loop with its former daemon-server semantics: absolute deadline, bounded
polls, no blind whole-frame retry, zero-byte writes are a broken pipe.
"""

from __future__ import annotations

import select
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LocalIpcPeer:
    """The operating-system identity of one accepted local connection.

    ``uid_unreadable`` records a credential the platform exposed but could
    not read or decode.  The seam only records it -- accept never fails on
    it -- so responsibility is explicit: a caller that must refuse unreadable
    credentials (the bootstrap listener) checks ``uid_unreadable`` itself and
    decides.  Judging by ``uid is None`` alone is wrong usage: a platform
    that simply has no uid for this transport also reports ``uid is None``
    with ``uid_unreadable`` False.
    """

    pid: int | None
    uid: int | None
    same_user_enforced: bool
    uid_unreadable: bool = False


class LocalIpcStream:
    """One local stream: a raw transport behind the seam's uniform surface."""

    def __init__(self, transport: Any) -> None:
        self._transport = transport

    def settimeout(self, timeout: float | None) -> None:
        self._transport.settimeout(timeout)

    def recv(self, size: int) -> bytes:
        return self._transport.recv(size)

    def sendall(self, data: bytes) -> None:
        self._transport.sendall(data)

    def send_response(
        self,
        data: bytes,
        *,
        timeout: float,
        poll_interval: float,
        should_stop: Callable[[], bool],
    ) -> bool:
        """Write one frame with known progress; never retry a whole sendall."""
        native: Callable[..., bool] | None = getattr(
            self._transport, "send_response", None
        )
        if native is not None:
            return native(
                data,
                timeout=timeout,
                poll_interval=poll_interval,
                should_stop=should_stop,
            )
        deadline = time.monotonic() + timeout
        offset = 0
        view = memoryview(data)
        while offset < len(view):
            if should_stop():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("daemon IPC response write deadline expired")
            self._transport.settimeout(min(poll_interval, remaining))
            try:
                count = self._transport.send(view[offset:])
            except TimeoutError:
                continue
            if count == 0:
                raise BrokenPipeError("daemon IPC response wrote zero bytes")
            offset += count
        return True

    def peer_disconnected(self) -> bool:
        """Whether the peer already closed, probed without consuming bytes.

        Any probe failure counts as disconnected; on a readable socket a
        zero-byte peek is the kernel's hangup report.
        """
        if not isinstance(self._transport, socket.socket):
            return False
        try:
            readable, _, _ = select.select((self._transport,), (), (), 0)
            if not readable:
                return False
            return self._transport.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def shutdown(self, how: int) -> None:
        self._transport.shutdown(how)

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> LocalIpcStream:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()


class LocalIpcListener:
    """One listening endpoint: a backend listener plus its peer reader."""

    def __init__(
        self, listener: Any, peer_factory: Callable[[Any], LocalIpcPeer]
    ) -> None:
        self._listener = listener
        self._peer_factory = peer_factory

    def accept(self) -> tuple[LocalIpcStream, LocalIpcPeer]:
        connection, _address = self._listener.accept()
        try:
            peer = self._peer_factory(connection)
        except BaseException:
            connection.close()
            raise
        return LocalIpcStream(connection), peer

    def settimeout(self, timeout: float | None) -> None:
        self._listener.settimeout(timeout)

    def close(self) -> None:
        self._listener.close()
