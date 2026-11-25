# Decision record (manysys/ipc/windows, W1-MANYSYS 2026-11-21):
# - Migrated from kernel/impl/platform/local_ipc/windows.py into manysys/ipc.
# - Lazily imports manysys.ipc.windows_pipe at listen/connect time so pywin32
#   named-pipe bindings stay unimported on POSIX.
"""The Windows named-pipe backend: the existing listener behind the seam.

Decision record (ipc-seam 2026-11-19): this adapter lazily loads
``kernel/impl/manysys/ipc/windows_pipe`` (which stays unimported on POSIX) and
presents it as a local IPC backend.  The pipe's DACL grants only the current
user plus an explicit desktop-launcher capability, so the accepted peer reports
``same_user_enforced=True`` with no OS credentials; ``backlog`` and ``clock``
have no pipe equivalent and are accepted unused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hyprial.kernel.impl.manysys.ipc.streams import (
    LocalIpcListener,
    LocalIpcPeer,
    LocalIpcStream,
)

_PIPE_PEER = LocalIpcPeer(pid=None, uid=None, same_user_enforced=True)


def _pipe_peer(_connection: Any) -> LocalIpcPeer:
    return _PIPE_PEER


class WindowsPipeBackend:
    """The current-user named pipe behind the local IPC seam."""

    def listen(
        self, endpoint: Path, *, backlog: int, gui_write_sid: str | None
    ) -> LocalIpcListener:
        from hyprial.kernel.impl.manysys.ipc.windows_pipe import PipeListener

        return LocalIpcListener(
            PipeListener(endpoint, gui_write_sid=gui_write_sid),
            peer_factory=_pipe_peer,
        )

    def connect(
        self, endpoint: Path, timeout: float, *, clock: Any | None = None
    ) -> LocalIpcStream:
        from hyprial.kernel.impl.manysys.ipc.windows_pipe import connect

        return LocalIpcStream(connect(endpoint, timeout))

    def path_limit(self) -> int | None:
        return None
