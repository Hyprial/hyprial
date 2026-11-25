# Decision record (manysys/ipc/servers, W1-MANYSYS 2026-11-21):
# - Migrated from kernel/impl/platform/local_ipc/servers.py into manysys/ipc.
"""The socketserver base for serving on the local IPC transport.

Decision record (ipc-channels 2026-11-19): the orgfs web server terminates
TLS on a unix-socket listener built from ``socketserver``; that base class
is a raw local-IPC capability, so it is served by the seam like every other
endpoint.  POSIX receives the stdlib unix stream server shape unchanged (the
caller still owns endpoint preparation and its permission checks); Windows
receives a clear ``OSError`` instead of the ``AttributeError`` a missing
``AF_UNIX`` would raise while the server class is being built.
"""

from __future__ import annotations

import socketserver
import sys


def local_ipc_stream_server_base() -> type[socketserver.BaseServer]:
    """The ``socketserver`` stream base bound to the platform's local transport.

    The returned class is the stdlib unix stream server on POSIX; calling
    this on Windows raises ``OSError`` because serving on a unix socket is a
    POSIX-only capability that must fail explicitly.
    """

    if sys.platform == "win32":
        raise OSError(
            "serving on a local unix socket is not supported on this platform"
        )
    return socketserver.UnixStreamServer
