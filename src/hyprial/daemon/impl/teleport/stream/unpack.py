"""Extract a teleport bundle tar into a fresh directory, validating every member.

Decisions:

- ``tarfile.extractall`` is never used.  Each member is checked before a byte
  is written: only regular files, relative names without empty, ``.`` or
  ``..`` components, no duplicates, and the running sum of declared sizes must
  stay within ``total``.
- Files are created with ``O_EXCL | O_NOFOLLOW`` and their mode is set with
  ``chmod`` so the process umask cannot change it; only permission bits
  (0777) are restored, never setuid/setgid/sticky.  Directories are 0700.
- ``dest`` must not exist.  Any rejection removes the ``dest`` this call
  created and raises ``TELEPORT_DIGEST_MISMATCH``; I/O errors unrelated to
  the stream content also clean up but propagate unchanged.
"""

from __future__ import annotations

import os
import shutil
import tarfile
from pathlib import Path, PurePosixPath

from hyprial.daemon.impl.teleport.errors import TeleportError
from hyprial.kernel import harden_private_path

_COPY = 1 << 20
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _reject(message: str) -> TeleportError:
    return TeleportError("TELEPORT_DIGEST_MISMATCH", message)


def _safe_parts(name: str) -> tuple[str, ...]:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise _reject(f"unsafe member name: {name!r}")
    parts = PurePosixPath(name).parts
    if name.endswith("/") or any(part in ("", ".", "..") for part in name.split("/")):
        raise _reject(f"unsafe member name: {name!r}")
    return parts


def _make_dirs(dest: Path, parts: tuple[str, ...]) -> Path:
    current = dest
    for part in parts[:-1]:
        current = current / part
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            if current.is_symlink() or not current.is_dir():
                raise _reject(f"member path collides with a file: {current.name!r}")
        else:
            harden_private_path(current, mode=0o700)
    return current / parts[-1]


def _write_member(archive: tarfile.TarFile, member: tarfile.TarInfo, path: Path) -> None:
    source = archive.extractfile(member)
    if source is None:
        raise _reject(f"unreadable member: {member.name!r}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        written = 0
        while block := source.read(_COPY):
            out.write(block)
            written += len(block)
        if written != member.size:
            raise _reject(f"short member: {member.name!r}")
        out.flush()
        os.fsync(out.fileno())
    os.chmod(path, member.mode & 0o777)


def _extract(source: Path, dest: Path, total: int) -> None:
    seen: set[str] = set()
    used = 0
    with tarfile.open(source, "r:") as archive:
        for member in archive:
            if not member.isreg():
                raise _reject(f"member is not a regular file: {member.name!r}")
            parts = _safe_parts(member.name)
            key = "/".join(parts)
            if key in seen:
                raise _reject(f"duplicate member: {member.name!r}")
            used += member.size
            if used > total:
                raise _reject("stream holds more bytes than declared total")
            path = _make_dirs(dest, parts)
            seen.add(key)
            _write_member(archive, member, path)
    if not seen:
        raise _reject("stream holds no members")


def unpack_stream(
    path: os.PathLike[str] | str, dest: os.PathLike[str] | str, *, total: int
) -> None:
    source = Path(path)
    target = Path(dest)
    os.mkdir(target, 0o700)
    harden_private_path(target, mode=0o700)
    try:
        _extract(source, target, total)
    except TeleportError:
        shutil.rmtree(target, ignore_errors=True)
        raise
    except (tarfile.TarError, EOFError, FileExistsError, NotADirectoryError, IsADirectoryError) as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise _reject(f"invalid stream: {exc}") from exc
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
