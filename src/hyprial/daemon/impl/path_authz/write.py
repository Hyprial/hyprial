# Decision record (W1E-2, 2026-11-21):
# - Preserves 100% of PathPolicy authorization rules (workspace/cwd containment
#   for sessions; hyprial home, other agent homes, secrets/, and daemon state_dir
#   always refused).
# - Removes _open_parent (O_PATH|O_DIRECTORY|O_NOFOLLOW dir_fd traversal),
#   _descriptor_path readback (which unconditionally refused all writes on
#   Windows, I1), and O_NOFOLLOW/O_BINARY flags in favor of realpath + _decide +
#   parent/target lstat & manysys.path_is_reparse verification + unbuffered
#   binary open + os.fstat regular-file, hardlink, and (st_dev, st_ino) checks
#   before truncation and write.
"""Who may make the daemon write which local file, and how it is written.

The write-side twin of :func:`read_authorized_file` (#1169, hq 2026-10-06):
the same :class:`PathPolicy` decides, so a caller may only write where it may
read -- its own workspace and registered cwd for a session, anywhere outside
the hyprial home and the state dir for the operator.

Mechanics: the parent directory is resolved with ``realpath`` and the target
is judged as ``<real parent>/<name>``.  Every resolved parent component and
any existing destination are checked via ``lstat`` and ``path_is_reparse``;
the regular file is opened in unbuffered binary mode and its descriptor is
verified via ``fstat`` (regular file, non-reparse, ``st_nlink == 1``, and
matching ``(st_dev, st_ino)``) before truncating or writing any byte.
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
    _is_link_or_reparse,
    _real,
)
from hyprial.kernel import path_is_reparse

REASON_EXISTS = "exists"


def _create_0600(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


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


def _verify_parent(parent: Path, target: Path, policy: PathPolicy) -> os.stat_result:
    observed_parent: os.stat_result | None = None
    for directory in reversed((parent, *parent.parents)):
        try:
            observed = os.lstat(directory)
        except OSError as error:
            raise PathRefused(
                REASON_UNREADABLE,
                f"the destination directory for {target} cannot be opened",
                path=str(target),
            ) from error
        if stat.S_ISLNK(observed.st_mode) or path_is_reparse(observed):
            _decide(_real(parent) / target.name, policy)
            raise PathRefused(
                REASON_SYMLINK,
                f"{target} has a symlink or reparse parent",
                path=str(target),
            )
        if not stat.S_ISDIR(observed.st_mode):
            raise PathRefused(
                REASON_UNREADABLE,
                f"the destination directory for {target} cannot be opened",
                path=str(target),
            )
        observed_parent = observed
    _decide(_real(parent) / target.name, policy)
    assert observed_parent is not None
    return observed_parent


def _validate_existing_target(observed: os.stat_result, target: Path, *, overwrite: bool) -> None:
    if stat.S_ISLNK(observed.st_mode) or path_is_reparse(observed):
        raise PathRefused(
            REASON_SYMLINK, f"{target} is a symlink", path=str(target)
        )
    if not overwrite:
        raise PathRefused(
            REASON_EXISTS, f"{target} already exists", path=str(target)
        )
    if not stat.S_ISREG(observed.st_mode):
        raise PathRefused(
            REASON_NOT_REGULAR, f"{target} is not a regular file", path=str(target)
        )
    if observed.st_nlink > 1:
        raise PathRefused(
            REASON_HARDLINK,
            "a multiply-linked destination cannot be overwritten",
            path=None,
        )


def authorize_write_path(
    raw_path: str | os.PathLike[str], policy: PathPolicy
) -> Path:
    """Judge a destination and its parent without creating the file."""

    _raw, parent, _name, target = _target(raw_path, policy)
    _verify_parent(parent, target, policy)
    return target


def write_authorized_file(
    raw_path: str | os.PathLike[str],
    data: bytes,
    policy: PathPolicy,
    *,
    overwrite: bool,
) -> Path:
    """Authorize ``raw_path`` for ``policy`` and write ``data`` through one fd.

    Raises :class:`PathRefused`.  A refusal writes no data: an existing file
    is truncated only after the descriptor's identity is verified.
    """

    _raw, parent, _name, target = _target(raw_path, policy)
    parent_before = _verify_parent(parent, target, policy)
    try:
        before_open: os.stat_result | None = os.lstat(target)
    except FileNotFoundError:
        before_open = None
    except OSError as error:
        raise PathRefused(
            REASON_UNREADABLE, f"{target} cannot be opened: {error}", path=str(target)
        ) from error

    if before_open is not None:
        _validate_existing_target(before_open, target, overwrite=overwrite)
        try:
            handle = open(target, "r+b", buffering=0)
        except OSError as error:
            if _is_link_or_reparse(target):
                raise PathRefused(
                    REASON_SYMLINK, f"{target} is a symlink", path=str(target)
                ) from error
            raise PathRefused(
                REASON_UNREADABLE, f"{target} cannot be opened: {error}", path=str(target)
            ) from error
    else:
        try:
            handle = open(target, "xb", buffering=0, opener=_create_0600)
        except FileExistsError as error:
            if _is_link_or_reparse(target):
                raise PathRefused(
                    REASON_SYMLINK, f"{target} is a symlink", path=str(target)
                ) from error
            if not overwrite:
                raise PathRefused(
                    REASON_EXISTS, f"{target} already exists", path=str(target)
                ) from error
            try:
                before_open = os.lstat(target)
            except OSError as stat_error:
                raise PathRefused(
                    REASON_UNREADABLE,
                    f"{target} cannot be opened: {stat_error}",
                    path=str(target),
                ) from stat_error
            _validate_existing_target(before_open, target, overwrite=True)
            try:
                handle = open(target, "r+b", buffering=0)
            except OSError as open_error:
                raise PathRefused(
                    REASON_UNREADABLE,
                    f"{target} cannot be opened: {open_error}",
                    path=str(target),
                ) from open_error
        except OSError as error:
            if _is_link_or_reparse(target):
                raise PathRefused(
                    REASON_SYMLINK, f"{target} is a symlink", path=str(target)
                ) from error
            raise PathRefused(
                REASON_UNREADABLE, f"{target} cannot be opened: {error}", path=str(target)
            ) from error

    with handle:
        fd = handle.fileno()
        opened = os.fstat(fd)
        if path_is_reparse(opened):
            raise PathRefused(
                REASON_SYMLINK, f"{target} is a symlink", path=str(target)
            )
        if not stat.S_ISREG(opened.st_mode):
            raise PathRefused(
                REASON_NOT_REGULAR, f"{target} is not a regular file", path=str(target)
            )
        parent_after = _verify_parent(parent, target, policy)
        if (parent_after.st_dev, parent_after.st_ino) != (
            parent_before.st_dev,
            parent_before.st_ino,
        ):
            raise PathRefused(
                REASON_UNVERIFIED,
                "the destination directory's real location could not be verified",
                path=None,
            )
        try:
            current_target = os.lstat(target)
        except OSError as error:
            raise PathRefused(
                REASON_UNVERIFIED,
                "the opened file's real location could not be verified",
                path=None,
            ) from error
        if stat.S_ISLNK(current_target.st_mode) or path_is_reparse(current_target):
            raise PathRefused(
                REASON_SYMLINK, f"{target} is a symlink", path=str(target)
            )
        if (opened.st_dev, opened.st_ino) != (
            current_target.st_dev,
            current_target.st_ino,
        ):
            raise PathRefused(
                REASON_UNVERIFIED,
                "the opened file's real location could not be verified",
                path=None,
            )
        if before_open is not None and (opened.st_dev, opened.st_ino) != (
            before_open.st_dev,
            before_open.st_ino,
        ):
            raise PathRefused(
                REASON_UNVERIFIED,
                "the opened file's real location could not be verified",
                path=None,
            )
        if opened.st_nlink > 1:
            raise PathRefused(
                REASON_HARDLINK,
                "a multiply-linked destination cannot be overwritten",
                path=None,
            )
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    return target


__all__ = [
    "REASON_EXISTS",
    "REASON_HARDLINK",
    "authorize_write_path",
    "write_authorized_file",
]
