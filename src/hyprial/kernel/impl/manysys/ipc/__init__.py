# Decision record (manysys/ipc, W1-MANYSYS 2026-11-21):
# - Migrated from kernel/impl/platform/local_ipc/__init__.py into manysys/ipc.
# - Single local IPC seam across AF_UNIX and Windows named pipes, with
#   process-wide mutually excluded backend injection for non-socket tests and
#   future Rust replacement.
"""One local IPC seam: listen and connect over replaceable backends.

Decision record (ipc-seam 2026-11-19, review round 1): business code calls
``listen_local_ipc``/``connect_local_ipc`` and never sees AF_UNIX versus a
named pipe.  The backend is chosen per call, so a test (or a later native
backend) can inject its own through ``local_ipc_backend`` without the entry
points touching ``socket.AF_UNIX`` at all.  The entry points own the shared
endpoint lifecycle that used to sit inline in the daemon server -- a fresh
``0700`` parent and stale endpoint removal -- while endpoint hardening is
backend-owned at the moment the backend materializes the endpoint (the
POSIX backend ``0600``-hardens between bind and listen, the daemon's exact
pre-seam order; the Windows pipe keeps its DACL-restricted endpoint
document unchanged).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sys
import threading
from typing import Any, Protocol

from hyprial.kernel.impl.manysys.ipc.posix import peer_process_id
from hyprial.kernel.impl.manysys.ipc.servers import local_ipc_stream_server_base
from hyprial.kernel.impl.manysys.ipc.streams import (
    LocalIpcListener,
    LocalIpcPeer,
    LocalIpcStream,
)

__all__ = [
    "LocalIpcBackend",
    "LocalIpcListener",
    "LocalIpcPeer",
    "LocalIpcStream",
    "active_local_ipc_backend",
    "connect_local_ipc",
    "listen_local_ipc",
    "local_ipc_backend",
    "local_ipc_path_limit",
    "local_ipc_stream_server_base",
    "peer_process_id",
]


class LocalIpcBackend(Protocol):
    """The transport contract behind the seam's entry points."""

    def listen(
        self, endpoint: Path, *, backlog: int, gui_write_sid: str | None
    ) -> LocalIpcListener: ...

    def connect(
        self, endpoint: Path, timeout: float, *, clock: Any | None = None
    ) -> LocalIpcStream: ...

    def path_limit(self) -> int | None: ...


_injected_backend: LocalIpcBackend | None = None
_injection_lock = threading.Lock()


def _default_backend() -> LocalIpcBackend:
    if sys.platform == "win32":
        from hyprial.kernel.impl.manysys.ipc.windows import WindowsPipeBackend

        return WindowsPipeBackend()
    from hyprial.kernel.impl.manysys.ipc.posix import PosixUnixBackend

    return PosixUnixBackend()


def active_local_ipc_backend() -> LocalIpcBackend:
    """The injected backend when one is active, else the platform default."""
    if _injected_backend is not None:
        return _injected_backend
    return _default_backend()


@contextmanager
def local_ipc_backend(
    backend: LocalIpcBackend | None,
) -> Iterator[LocalIpcBackend | None]:
    """Inject a backend for the block's duration; ``None`` forces default choice."""
    global _injected_backend
    with _injection_lock:
        if _injected_backend is not None and backend is not _injected_backend:
            raise RuntimeError(
                "a different local IPC backend is already injected; "
                "concurrent different injections are not supported"
            )
        previous = _injected_backend
        _injected_backend = backend
    try:
        yield backend
    finally:
        with _injection_lock:
            if _injected_backend is backend:
                _injected_backend = previous


def listen_local_ipc(
    endpoint: Path, *, backlog: int = 32, gui_write_sid: str | None = None
) -> LocalIpcListener:
    """Listen at ``endpoint``: fresh ``0700`` parent, no stale endpoint."""
    endpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    endpoint.unlink(missing_ok=True)
    return active_local_ipc_backend().listen(
        endpoint, backlog=backlog, gui_write_sid=gui_write_sid
    )


def connect_local_ipc(
    endpoint: Path, timeout: float, *, clock: Any | None = None
) -> LocalIpcStream:
    """Connect to the listener at ``endpoint`` within ``timeout``."""
    return active_local_ipc_backend().connect(endpoint, timeout, clock=clock)


def local_ipc_path_limit() -> int | None:
    """The longest endpoint path this transport accepts, or ``None`` unbounded."""
    return active_local_ipc_backend().path_limit()
