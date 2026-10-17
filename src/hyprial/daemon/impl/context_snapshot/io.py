"""No-follow bounded reads and atomic private handoff publication."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

from hyprial.daemon.impl.context_snapshot.contract import ContextSnapshotError


@dataclass(frozen=True, slots=True)
class BoundedLines:
    lines: tuple[str, ...]
    digest: str
    captured_bytes: int
    source_bytes: int
    start_offset: int
    byte_truncated: bool
    torn_final_line: bool
    invalid_utf8: bool


def _relative_ref(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as error:
        raise ContextSnapshotError("snapshot source is outside the agent home") from error


def _reject_symlink_components(root: Path, path: Path) -> None:
    relative = Path(_relative_ref(root, path))
    current = root
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise ContextSnapshotError(
                f"snapshot source symlink is refused: {relative.as_posix()}"
            )


def read_bounded_lines(root: Path, path: Path, limit: int) -> BoundedLines:
    """Read at most ``limit`` tail bytes, returning only complete UTF-8 lines."""

    _reject_symlink_components(root, path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise ContextSnapshotError("snapshot source cannot be opened safely") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ContextSnapshotError("snapshot source is not a regular file")
        source_bytes = metadata.st_size
        requested_start = max(0, source_bytes - limit)
        os.lseek(descriptor, requested_start, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = limit
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)

    start_offset = requested_start
    byte_truncated = requested_start > 0
    if byte_truncated:
        newline = payload.find(b"\n")
        if newline < 0:
            payload = b""
            start_offset = source_bytes
        else:
            payload = payload[newline + 1 :]
            start_offset += newline + 1
    torn_final_line = bool(payload) and not payload.endswith(b"\n")
    if torn_final_line:
        payload = payload.rsplit(b"\n", 1)[0]
        if payload:
            payload += b"\n"
    invalid_utf8 = False
    lines: list[str] = []
    for raw in payload.splitlines():
        try:
            lines.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            invalid_utf8 = True
    return BoundedLines(
        lines=tuple(lines),
        digest=hashlib.sha256(payload).hexdigest(),
        captured_bytes=len(payload),
        source_bytes=source_bytes,
        start_offset=start_offset,
        byte_truncated=byte_truncated,
        torn_final_line=torn_final_line,
        invalid_utf8=invalid_utf8,
    )


def relative_ref(root: Path, path: Path) -> str:
    return _relative_ref(root, path)


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ContextSnapshotError("handoff publication directory is unsafe")
    os.chmod(path, 0o700)


def _write_private(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as target:
        os.fchmod(target.fileno(), 0o600)
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_handoff(
    home: Path,
    switch_id: str,
    *,
    manifest: bytes,
    body: bytes,
    before_publish: Callable[[Path, Path], None] | None = None,
) -> Path:
    """Publish both files with one directory rename; incomplete stages stay hidden."""

    context = home / "state" / "context"
    parent = context / "handoff"
    _ensure_private_directory(context)
    _ensure_private_directory(parent)
    target = parent / switch_id
    if target.exists() or target.is_symlink():
        raise ContextSnapshotError("handoff snapshot already exists and is immutable")
    stage = parent / f".{switch_id}.{uuid4().hex}.tmp"
    stage.mkdir(mode=0o700)
    try:
        _write_private(stage / "manifest.json", manifest)
        _write_private(stage / "handoff.md", body)
        _fsync_directory(stage)
        if before_publish is not None:
            before_publish(stage, target)
        os.rename(stage, target)
        _fsync_directory(parent)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return target


__all__ = ["BoundedLines", "publish_handoff", "read_bounded_lines", "relative_ref"]
