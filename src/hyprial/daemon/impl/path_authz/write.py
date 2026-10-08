"""Who may make the daemon write which local file, and how it is written.

The write-side twin of :func:`read_authorized_file` (#1169, hq 2026-10-06):
the same :class:`PathPolicy` decides, so a caller may only write where it may
read -- its own workspace and registered cwd for a session, anywhere outside
the hyprial home and the state dir for the operator.

Mechanics: the parent directory is resolved with ``realpath`` and the target
is judged as ``<real parent>/<name>``.  Every resolved parent component is
then opened descriptor-relatively with ``O_DIRECTORY | O_NOFOLLOW``; the
parent descriptor's own path is re-judged before the final name is opened
relative to it.  The file uses ``O_NOFOLLOW | O_CREAT`` (and ``O_EXCL`` unless
overwriting), and its descriptor path is re-judged before any byte is written.
Where the platform cannot report either descriptor path the write is refused.

Known corner (review 1012, not an escalation): a directory already pinned by
the walk can still be renamed into a denied location by someone who can write
there; the O_CREAT then lands an empty file at its new place before the file
descriptor's path is re-judged and the write refused.  No caller bytes reach
it, and the renamer needed write access to that location anyway.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from hyprial.daemon.impl.path_authz.policy import (
    REASON_HARDLINK,
    REASON_NOT_REGULAR,
    REASON_OUTSIDE_ALLOWED_ROOTS,
    REASON_SYMLINK,
    REASON_UNREADABLE,
    REASON_UNVERIFIED,
    PathPolicy,
    PathRefused,
    _decide,
    _descriptor_path,
    _real,
)

REASON_EXISTS = "exists"

# Directories are walked search-only (O_PATH on Linux, O_SEARCH on macOS):
# a component the caller may traverse but not list (mode --x) is legitimate,
# and reading a directory's entries is never needed to open a name in it.
_DIRECTORY_FLAGS = (
    (getattr(os, "O_PATH", 0) or getattr(os, "O_SEARCH", 0) or os.O_RDONLY)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)

_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    # A FIFO with no reader would block the daemon thread in open(); with
    # O_NONBLOCK it fails (ENXIO) and anything non-regular is refused below.
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOCTTY", 0)
    | getattr(os, "O_BINARY", 0)
)


def _target(
    raw_path: str | os.PathLike[str], policy: PathPolicy
) -> tuple[str, Path, str, Path]:
    raw = os.path.expanduser(os.fspath(raw_path))
    if not os.path.isabs(raw):
        raise PathRefused(
            REASON_OUTSIDE_ALLOWED_ROOTS,
            f"{os.fspath(raw_path)!r} must be an absolute path",
            path=os.fspath(raw_path),
        )
    name = os.path.basename(raw)
    if name in {"", ".", ".."}:
        raise PathRefused(
            REASON_NOT_REGULAR, f"{raw!r} does not name a file", path=raw
        )
    parent = _real(os.path.dirname(raw))
    target = parent / name
    _decide(target, policy)
    return raw, parent, name, target


def _open_parent(parent: Path, target: Path, policy: PathPolicy) -> int:
    """Open every resolved component without following a replacement link."""

    current: int | None = None
    try:
        current = os.open(parent.anchor, _DIRECTORY_FLAGS)
        for component in parent.parts[1:]:
            following = os.open(
                component,
                _DIRECTORY_FLAGS,
                dir_fd=current,
            )
            os.close(current)
            current = following
        actual = _descriptor_path(current)
        if actual is None:
            raise PathRefused(
                REASON_UNVERIFIED,
                "the destination directory's real location could not be verified",
                path=None,
            )
        _decide(actual, policy)
        return current
    except PathRefused:
        if current is not None:
            os.close(current)
        raise
    except OSError as error:
        if current is not None:
            os.close(current)
        raise PathRefused(
            REASON_UNREADABLE,
            f"the destination directory for {target} cannot be opened",
            path=str(target),
        ) from error


def authorize_write_path(
    raw_path: str | os.PathLike[str], policy: PathPolicy
) -> Path:
    """Judge a destination and its opened parent without creating the file."""

    _raw, parent, _name, target = _target(raw_path, policy)
    directory_fd = _open_parent(parent, target, policy)
    os.close(directory_fd)
    return target


def _is_symlink_at(directory_fd: int, name: str) -> bool:
    try:
        opened = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISLNK(opened.st_mode)


def write_authorized_file(
    raw_path: str | os.PathLike[str],
    data: bytes,
    policy: PathPolicy,
    *,
    overwrite: bool,
) -> Path:
    """Authorize ``raw_path`` for ``policy`` and write ``data`` through one fd.

    Raises :class:`PathRefused`.  A refusal writes no data: an existing file
    is truncated only after the descriptor's location is verified.  A file
    this call created and then refused is left empty rather than unlinked by
    path, since the path may no longer name the file the descriptor holds.
    """

    _raw, parent, name, target = _target(raw_path, policy)
    flags = _WRITE_FLAGS if overwrite else _WRITE_FLAGS | os.O_EXCL
    directory_fd = _open_parent(parent, target, policy)
    try:
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except FileExistsError as error:
        raise PathRefused(
            REASON_EXISTS, f"{target} already exists", path=str(target)
        ) from error
    except OSError as error:
        if _is_symlink_at(directory_fd, name):
            raise PathRefused(
                REASON_SYMLINK, f"{target} is a symlink", path=str(target)
            ) from error
        raise PathRefused(
            REASON_UNREADABLE, f"{target} cannot be opened: {error}", path=str(target)
        ) from error
    finally:
        os.close(directory_fd)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise PathRefused(
                REASON_NOT_REGULAR, f"{target} is not a regular file", path=str(target)
            )
        actual = _descriptor_path(fd)
        if actual is None:
            # Unlike a read, a write cannot fall back to an inode comparison
            # with a file it is creating: no reported path, no write.
            raise PathRefused(
                REASON_UNVERIFIED,
                "the opened file's real location could not be verified",
                path=None,
            )
        _decide(actual, policy)
        if opened.st_nlink > 1:
            # A second name can live in a denied root while this descriptor's
            # reported path is allowed.  Never truncate a multiply-linked file
            # and never echo the apparently harmless alias: it names a secret.
            raise PathRefused(
                REASON_HARDLINK,
                "a multiply-linked destination cannot be overwritten",
                path=None,
            )
        os.ftruncate(fd, 0)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return target


__all__ = [
    "REASON_EXISTS",
    "REASON_HARDLINK",
    "authorize_write_path",
    "write_authorized_file",
]
