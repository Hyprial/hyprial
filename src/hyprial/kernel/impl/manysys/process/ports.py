# Decision record (manysys/process/ports, W1-MANYSYS & W2B-1 2026-11-21):
# - Migrated from kernel/impl/platform/ports.py into manysys/process.
# - Extended in W2B-1 with executable_filename, open_descriptor_count,
#   posix_runtime_bus_dir, and GUEST_WORKER_STATE so caller modules do not carry
#   platform branches or POSIX path literals outside manysys.
"""Public capability ports for platform-owned process, path and I/O behavior."""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path, PureWindowsPath
import sys
from typing import TYPE_CHECKING

from hyprial.kernel.impl.manysys.fs.account import current_uid

if TYPE_CHECKING:
    from hyprial.kernel.impl.manysys.process.owned_process import OwnedProcessGroup

GUEST_WORKER_STATE = "/run/hyprial-worker"


def create_owned_process_group(*, label: str) -> OwnedProcessGroup:
    """Construct the native ownership handle without changing its label."""
    from hyprial.kernel.impl.manysys.process.owned_process import OwnedProcessGroup

    if sys.platform == "win32":
        from hyprial.kernel.impl.manysys.process.windows_owned_process import (
            WindowsOwnedProcessGroup,
        )

        return WindowsOwnedProcessGroup(label=label)
    return OwnedProcessGroup(label=label)


def is_windows_owned_process_group(process_group: object) -> bool:
    """Keep native class identity checks at their implementation owner."""
    if sys.platform != "win32":
        return False
    from hyprial.kernel.impl.manysys.process.windows_owned_process import (
        WindowsOwnedProcessGroup,
    )

    return isinstance(process_group, WindowsOwnedProcessGroup)


def loopback_inbound_supported() -> bool:
    """Whether managed forwarding uses the Windows loopback TCP transport."""
    return sys.platform == "win32"


def file_modes_are_significant() -> bool:
    """Whether a checkout preserves the executable mode bit as tracked content."""
    return sys.platform != "win32"


def executable_filename(stem: str) -> str:
    """Return ``stem`` with ``.exe`` on Windows and unchanged elsewhere."""
    return f"{stem}.exe" if sys.platform == "win32" else stem


def open_descriptor_count() -> int | None:
    """Return open file descriptor count where ``/dev/fd`` is available, else ``None``."""
    if sys.platform == "win32" or os.name == "nt":
        return None
    try:
        return len(os.listdir("/dev/fd")) - 1
    except OSError:
        return None


def posix_runtime_bus_dir(uid: int | None = None) -> Path | None:
    """Return ``/run/user/<uid>`` on POSIX hosts, or ``None`` on Windows."""
    if sys.platform == "win32" or os.name == "nt":
        return None
    resolved_uid = current_uid() if uid is None else uid
    return Path(f"/run/user/{resolved_uid}")


def managed_launcher_names(name: str) -> tuple[str, str]:
    """npm-bin launcher candidates in this platform's preference order."""
    if sys.platform == "win32":
        return (f"{name}.cmd", name)
    return (name, f"{name}.cmd")


def isolated_launch_search_path() -> tuple[str, ...]:
    """The fixed isolated-launch search path; empty where POSIX dirs do not exist."""
    if sys.platform == "win32":
        return ()
    return ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def native_profile_environment(
    home: Path, source: Mapping[str, str]
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Native user-profile environment and search paths below a managed home."""
    if sys.platform != "win32":
        return {}, ()
    profile = PureWindowsPath(str(home))
    app_data = profile / "AppData"
    environment = {
        "USERPROFILE": str(profile),
        "APPDATA": str(app_data / "Roaming"),
        "LOCALAPPDATA": str(app_data / "Local"),
    }
    path_parts: list[str] = []
    system_root = source.get("SYSTEMROOT") or source.get("WINDIR")
    if system_root:
        root = PureWindowsPath(system_root)
        path_parts.extend([str(root), str(root / "System32")])
    for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
        value = source.get(key)
        if value:
            environment[key] = value
    return environment, tuple(path_parts)


def _require_windows() -> None:
    if sys.platform != "win32" and os.name != "nt":
        raise OSError("Windows named pipes are unavailable on this platform")


def windows_process_identity(pid: int) -> str:
    """Read the native birth identity at its platform owner."""
    _require_windows()
    from hyprial.kernel.impl.manysys.process.windows_process import process_identity

    return process_identity(pid)
