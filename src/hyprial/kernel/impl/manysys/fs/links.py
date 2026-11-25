# Decision record (manysys/fs/links, W1-MANYSYS 2026-11-21):
# - Centralizes directory link creation, inspection, and removal across POSIX
#   and Windows inside manysys so no upper-layer caller invokes os.symlink /
#   Path.symlink_to (forbidden by check_windows_portability W013) or _winapi
#   directly.
# - POSIX uses os.symlink(..., target_is_directory=True), S_ISLNK(lstat), and
#   os.unlink().
# - Windows uses _winapi.CreateJunction(str(target), str(link_path)), which
#   requires neither Administrator elevation nor Developer Mode, identifies
#   links via S_ISLNK(lstat) or path_is_reparse(lstat), and removes the
#   junction reparse point via os.rmdir() without deleting target contents.
# - unlink_directory_link verifies S_ISLNK or path_is_reparse via os.lstat
#   before removal so an ordinary empty directory is never accidentally removed
#   by os.rmdir().
"""Cross-platform directory links (POSIX directory symlinks, Windows junctions)."""

from __future__ import annotations

import errno
import importlib
import os
from pathlib import Path
import stat
import sys
from types import ModuleType

from .private_access import path_is_reparse


def _is_windows() -> bool:
    return os.name == "nt" or sys.platform == "win32"


def _winapi_module() -> ModuleType:
    return importlib.import_module("_winapi")


def link_directory(target: Path | str, link_path: Path | str) -> None:
    """Create a directory link at ``link_path`` pointing to ``target``."""

    if _is_windows():
        _winapi_module().CreateJunction(str(target), str(link_path))
        return
    os.symlink(os.fspath(target), os.fspath(link_path), target_is_directory=True)


def is_directory_link(path: Path | str) -> bool:
    """Return ``True`` when ``path`` is a symlink or Windows reparse point."""

    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return bool(stat.S_ISLNK(metadata.st_mode) or path_is_reparse(metadata))


def unlink_directory_link(path: Path | str) -> None:
    """Remove a directory link/junction without touching its target's contents."""

    metadata = os.lstat(path)
    if not (stat.S_ISLNK(metadata.st_mode) or path_is_reparse(metadata)):
        raise OSError(errno.EINVAL, "path is not a directory link", os.fspath(path))
    if _is_windows():
        os.rmdir(path)
        return
    os.unlink(path)
