# Decision record (manysys/ipc/posix, W1-MANYSYS 2026-11-21):
# - Migrated from kernel/impl/platform/local_ipc/posix.py into manysys/ipc.
# - Keeps bind -> chmod 0o600 -> listen ordering, EAGAIN/EWOULDBLOCK backlog
#   retry on connect, AF_UNIX path limit probing, and SO_PEERCRED / BSD
#   LOCAL_PEERCRED / Darwin LOCAL_PEERPID reading.
"""The POSIX AF_UNIX backend: bind/listen, patient connect, peer credentials.

Decision record (ipc-seam 2026-11-19, review round 1): the peer-pid reader
moved from ``daemon/impl/ipc/params.py`` and the peer-uid reader from
``daemon/impl/application/onboarding/bootstrap/peer.py``; the backlog-patient
connect moved from ``kernel/impl/ipc/client.py``; the AF_UNIX path-length probe
moved from ``shell/impl/cli/commands/common/daemon_start.py``.  Every piece
keeps its former semantics: the pid read is best effort, the uid reader still
treats an unreadable credential as ``OSError`` -- but the peer factory records
it as ``uid_unreadable`` instead of failing the accept, because the daemon
server never read uids here and the bootstrap refusal rule belongs to its own
slice.  Endpoint hardening stays at this backend, between bind and listen.
Only the full-backlog spelling (EAGAIN/EWOULDBLOCK) is retried inside the
connect budget.
"""

from __future__ import annotations

import errno
import os
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Any

from hyprial.kernel.impl.manysys.ipc.streams import (
    LocalIpcListener,
    LocalIpcPeer,
    LocalIpcStream,
)

_CONNECT_BACKLOG_ERRNOS = frozenset({errno.EAGAIN, errno.EWOULDBLOCK})

_CONNECT_BACKLOG_RETRY_INTERVAL = 0.01

_AF_UNIX_PROBE_DIRECTORY = "/hyprial-af-unix-limit-probe-nonexistent/"

_LOCAL_PEERCRED = 0x001
_XUCRED_SIZE = 76
_XUCRED_VERSION = 0


def peer_process_id(connection: socket.socket) -> int | None:
    """Return a Unix peer PID when the platform exposes one, else ``None``.

    Darwin exposes LOCAL_PEERPID in ``<sys/un.h>``, but Python's socket
    module does not publish the constants, so the Darwin probe uses the raw
    option number directly.
    """

    peer_credential = getattr(socket, "SO_PEERCRED", None)
    if peer_credential is not None:
        try:
            raw = connection.getsockopt(socket.SOL_SOCKET, peer_credential, 12)
            pid, _uid, _gid = struct.unpack("3i", raw)
        except (OSError, struct.error):
            return None
        return pid if pid > 0 else None
    if sys.platform == "darwin":
        try:
            raw = connection.getsockopt(0, 0x002, 4)
            (pid,) = struct.unpack("i", raw)
        except (OSError, struct.error):
            return None
        return pid if pid > 0 else None
    return None


def read_peer_uid(connection: socket.socket) -> int | None:
    """The effective uid of the process that connected, or ``None``.

    Where ``socket`` publishes ``SO_PEERCRED`` (Linux) it is the source and a
    failure to read or decode it raises ``OSError``: an unreadable credential
    is never an accepted one.  Elsewhere the BSD-family ``LOCAL_PEERCRED``
    probe (macOS, the ``getpeereid`` source) is tried; a platform on which
    that probe fails offers no credential, so ``None`` is returned.
    """

    peercred = getattr(socket, "SO_PEERCRED", None)
    if peercred is not None:
        raw = connection.getsockopt(socket.SOL_SOCKET, peercred, struct.calcsize("3I"))
        try:
            _pid, uid, _gid = struct.unpack("3I", raw)
        except struct.error as error:
            raise OSError("peer credential has an unexpected layout") from error
        return uid
    try:
        raw = connection.getsockopt(0, _LOCAL_PEERCRED, _XUCRED_SIZE)
    except OSError:
        return None
    try:
        version, uid = struct.unpack_from("II", raw)
    except struct.error as error:
        raise OSError("peer credential has an unexpected layout") from error
    if version != _XUCRED_VERSION:
        raise OSError("peer credential has an unsupported version")
    return uid


def _peer(connection: socket.socket) -> LocalIpcPeer:
    pid = peer_process_id(connection)
    try:
        uid = read_peer_uid(connection)
    except OSError:
        return LocalIpcPeer(
            pid=pid, uid=None, same_user_enforced=False, uid_unreadable=True
        )
    return LocalIpcPeer(pid=pid, uid=uid, same_user_enforced=False)


def af_unix_path_limit() -> int | None:
    """The longest AF_UNIX path (bytes) the platform accepts, measured.

    Binding below a directory that cannot exist fails with ENOENT while the
    path fits and with a length error (no errno) once it does not, so the
    limit is found without creating a file. ``None`` means no AF_UNIX support.
    """
    if not hasattr(socket, "AF_UNIX"):
        return None
    for length in range(len(_AF_UNIX_PROBE_DIRECTORY) + 1, 4096):
        candidate = _AF_UNIX_PROBE_DIRECTORY + "x" * (
            length - len(_AF_UNIX_PROBE_DIRECTORY)
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(candidate)
            except OSError as error:
                if error.errno is None:
                    return length - 1
    return None


class PosixUnixBackend:
    """AF_UNIX sockets behind the local IPC seam."""

    def listen(
        self, endpoint: Path, *, backlog: int, gui_write_sid: str | None
    ) -> LocalIpcListener:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(endpoint))
            os.chmod(endpoint, 0o600)
            server.listen(backlog)
        except BaseException:
            server.close()
            raise
        return LocalIpcListener(server, peer_factory=_peer)

    def connect(
        self, endpoint: Path, timeout: float, *, clock: Any | None = None
    ) -> LocalIpcStream:
        """Connect to a listening endpoint, waiting out a full accept queue.

        The kernel only parks a connect() against a full AF_UNIX accept queue
        for fully blocking sockets, and this caller always runs with a
        timeout, so patience has to be explicit.  Only the full-backlog
        spelling (EAGAIN/EWOULDBLOCK) is retried -- ECONNREFUSED still fails
        fast because it also describes a stale socket file whose daemon is
        gone, which must not take the whole budget to report.
        """
        active = time if clock is None else clock
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        deadline = active.monotonic() + timeout
        try:
            while True:
                try:
                    client.connect(str(endpoint))
                except OSError as error:
                    if (
                        error.errno in _CONNECT_BACKLOG_ERRNOS
                        and active.monotonic() < deadline
                    ):
                        active.sleep(_CONNECT_BACKLOG_RETRY_INTERVAL)
                        continue
                    raise
                return LocalIpcStream(client)
        except BaseException:
            client.close()
            raise

    def path_limit(self) -> int | None:
        return af_unix_path_limit()
