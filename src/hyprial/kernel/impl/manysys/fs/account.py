# Decision record (manysys/fs/account, W1-MANYSYS & W2B-1 2026-11-21):
# - Migrated from kernel/impl/platform/privacy/account.py into manysys/fs and
#   extended with apply_private_umask and host_machine_identifier.
# - apply_private_umask sets os.umask(mask) on POSIX and returns 0 as a no-op
#   on Windows (where CRT umask bits do not express multi-user ACL privacy).
# - host_machine_identifier centralizes OS machine identity discovery (Linux
#   /etc/machine-id and /var/lib/dbus/machine-id, Darwin IOPlatformUUID via
#   ioreg, Windows MachineGuid via winreg, plus hostname/node fallback) so
#   identity/device modules contain zero platform branches or winreg imports.
"""Current OS account facts, machine identifier, and process umask seam."""

from __future__ import annotations

import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import uuid


def current_uid() -> int:
    """Return this process's uid, or 0 where the platform has no ``os.getuid``.

    Windows has no POSIX uids: ``os.getuid`` does not exist and ``stat``
    reports uid 0 for every path.  Callers keep their ownership fences on
    POSIX; this function only removes the attribute-lookup failure.
    """
    getuid = getattr(os, "getuid", None)
    return 0 if getuid is None else getuid()


def posix_metadata_enforced() -> bool:
    """Whether ``stat`` uid and permission bits describe access control here.

    Windows synthesises ``st_mode`` bits (0o777 directories, 0o666 files) and
    ignores ``chmod``, so equality checks against POSIX modes are meaningless
    there; the account ACLs enforce access instead.  A platform without
    ``os.getuid`` cannot compare ownership either, so it is never enforced.
    """
    return os.name != "nt" and getattr(os, "getuid", None) is not None


def apply_private_umask(mask: int = 0o077) -> int:
    """Apply ``mask`` via ``os.umask`` on POSIX; return ``0`` no-op on Windows."""
    if os.name == "nt" or sys.platform == "win32":
        return 0
    return os.umask(mask)


def host_machine_identifier() -> tuple[str, str]:
    """Return ``(source, identifier)`` from the native host identity store."""
    system = platform.system().lower()
    if system == "linux":
        for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                value = path.read_text("utf-8").strip()
            except OSError:
                continue
            if value:
                return "linux-machine-id", value
    elif system == "darwin":
        try:
            completed = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                check=False,
                timeout=3,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            completed = None
        if completed is not None and completed.returncode == 0:
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', completed.stdout)
            if match is not None:
                return "darwin-platform-uuid", match.group(1)
    elif system == "windows":
        try:
            import winreg  # type: ignore[import-not-found]

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
            ) as key:
                value, _kind = winreg.QueryValueEx(key, "MachineGuid")
            if isinstance(value, str) and value:
                return "windows-machine-guid", value
        except (ImportError, OSError):
            pass
    return (
        "fallback-host-node",
        f"{socket.gethostname()}\0{uuid.getnode():012x}\0{os.name}",
    )
