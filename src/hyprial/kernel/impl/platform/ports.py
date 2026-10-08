"""Public construction ports for platform-owned process and named-pipe I/O."""

from __future__ import annotations

import sys
from pathlib import Path

from hyprial.kernel.impl.processes.owned_process import OwnedProcessGroup


def create_owned_process_group(*, label: str) -> OwnedProcessGroup:
    """Construct the native ownership handle without changing its label."""
    if sys.platform == "win32":
        from hyprial.kernel.impl.platform.windows_owned_process import WindowsOwnedProcessGroup

        return WindowsOwnedProcessGroup(label=label)
    return OwnedProcessGroup(label=label)


def is_windows_owned_process_group(process_group: object) -> bool:
    """Keep native class identity checks at their implementation owner."""
    if sys.platform != "win32":
        return False
    from hyprial.kernel.impl.platform.windows_owned_process import WindowsOwnedProcessGroup

    return isinstance(process_group, WindowsOwnedProcessGroup)


def _require_windows() -> None:
    if sys.platform != "win32":
        raise OSError("Windows named pipes are unavailable on this platform")


def connect_named_pipe(endpoint: Path, timeout: float):
    """Open the existing current-user native IPC stream."""
    _require_windows()
    from hyprial.kernel.impl.platform.windows_pipe import connect

    return connect(endpoint, timeout)


def listen_named_pipe(endpoint: Path, *, gui_write_sid: str | None = None):
    """Construct the existing native listener with unchanged access scope."""
    _require_windows()
    from hyprial.kernel.impl.platform.windows_pipe import PipeListener

    return PipeListener(endpoint, gui_write_sid=gui_write_sid)


def windows_process_identity(pid: int) -> str:
    """Read the native birth identity at its platform owner."""
    _require_windows()
    from hyprial.kernel.impl.platform.windows_process import process_identity

    return process_identity(pid)
