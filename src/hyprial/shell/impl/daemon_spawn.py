# Decision record (W2B-1, 2026-11-21):
# - Provides the shell-internal composition seam for launch-file hardening and
#   detached daemon spawning so CLI daemon launch helpers do not import kernel
#   manysys helpers directly across the CLI->backend import ratchet boundary.
# - Windows bootstrap ACL fix (PR #3106 acceptance, 2026-11-23): the launcher
#   used to create the state directory with ``mkdir(mode=0o700)`` and never
#   harden it.  CPython 3.12.4+ (gh-118486) on Windows turns that mode into its
#   own protected DACL (SYSTEM, Administrators, OWNER RIGHTS S-1-3-4) instead
#   of inheriting
#   the hardened home, so bootstrap admission rejected the new state directory
#   as ``unsafe-acl``.  ``prepare_private_state_dir`` creates every missing
#   component through ``secure_private_directory(create=True)``, the same
#   create-then-harden rule ``DaemonStateOwnershipFence`` uses.  An existing
#   state directory is left as it is (POSIX mode preserved) and is validated by
#   bootstrap admission and the ownership fence, never widened or rewritten
#   here.  The kernel validator separately accepts that CPython OWNER RIGHTS
#   DACL (see manysys/fs/private_access.py); create-then-harden still gives
#   the launcher's own state directory a current-user-only DACL.
# - Concurrent launchers (review N-1, 2026-11-23): ``FileExistsError`` can come
#   from an ancestor a racing launcher created first, while the state
#   directory itself is still missing.  The preparation is then repeated once;
#   a second failure is raised.  When the state directory exists after the
#   error, the racing launcher created it and it is left alone.
"""Shell composition bindings for daemon launch hardening and detached spawn."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any


def harden_private_path(path: Path, *, mode: int) -> None:
    """Harden a newly created private launch file through kernel manysys."""

    from hyprial.kernel import harden_private_path as _harden_private_path

    _harden_private_path(path, mode=mode)


def prepare_private_state_dir(path: Path) -> None:
    """Create a missing daemon state directory as private; keep an existing one.

    Every component this call creates is hardened (0o700 on POSIX, a
    protected current-user DACL on Windows).  A directory that already exists
    is not modified; later admission decides whether it is private.
    """

    from hyprial.kernel import secure_private_directory

    if os.path.lexists(path):
        return
    try:
        secure_private_directory(path, create=True, mode=0o700)
    except FileExistsError:
        if not os.path.lexists(path):
            secure_private_directory(path, create=True, mode=0o700)


def spawn_detached_daemon(args: Any, **popen_kwargs: Any) -> subprocess.Popen[Any]:
    """Spawn a detached daemon process through kernel manysys."""

    from hyprial.kernel import spawn_detached_daemon as _spawn_detached_daemon

    return _spawn_detached_daemon(args, **popen_kwargs)
