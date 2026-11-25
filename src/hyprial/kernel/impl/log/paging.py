# Decision record (W2B-2, 2026-11-21):
# - LogPager._open already validates root/parent/file symlink and reparse state,
#   containment under root, O_NOFOLLOW open flags, and post-open fstat (st_dev,
#   st_ino) snapshot identity, so the unguarded POSIX O_DIRECTORY dir_fd walk is
#   removed in favor of one portable os.open path.
"""Bounded local JSONL snapshot traversal; callers own authorization/projection.

The cursor is short-lived traversal metadata, never a durable log authority.
Files are traversed by relative filename then byte offset, not timestamp order.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from typing import Any

from . import PRE_TRAJECTORY_ARCHIVE
from hyprial.kernel.impl.manysys.interface import path_is_reparse


class LogCursorInvalid(ValueError):
    """The requested immutable snapshot is no longer available."""


@dataclass
class _File:
    path: Path
    device: int
    inode: int
    size: int
    mtime: int


@dataclass
class _Snapshot:
    created: float
    files: list[_File]
    reasons: set[str]


def _valid_entry(value: Any) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("ts"), str):
        return False
    try:
        stamp = datetime.fromisoformat(value["ts"])
    except ValueError:
        return False
    return (stamp.tzinfo is not None and isinstance(value.get("level"), str) and value["level"] in {"debug", "info", "warn", "error"}
            and isinstance(value.get("component"), str) and isinstance(value.get("event"), str))


class LogPager:
    """Read a caller-authorized root with bounded discovery, memory, and I/O."""

    def __init__(self, root: Path, *, byte_budget: int = 1_048_576,
                 line_bytes: int = 65_536, max_files: int = 128,
                 discovery_budget: int = 4096, ttl: float = 300.0) -> None:
        self.root = Path(root).absolute()
        if min(byte_budget, line_bytes, max_files, discovery_budget, ttl) <= 0:
            raise ValueError("positive log reader budgets required")
        self.byte_budget = byte_budget
        self.line_bytes = line_bytes
        self.max_files = max_files
        self.discovery_budget = discovery_budget
        self.ttl = ttl
        self._key = secrets.token_bytes(32)
        self._snapshots: dict[str, _Snapshot] = {}
        self._lock = threading.RLock()

    def _discover(self) -> _Snapshot:
        reasons: set[str] = set()
        files: list[_File] = []
        pending = [(self.root, 0)]
        visited = 0
        try:
            unavailable = (not self.root.is_dir() or self.root.is_symlink()
                           or path_is_reparse(self.root.lstat()))
        except OSError:
            unavailable = True
        if unavailable:
            return _Snapshot(time.monotonic(), [], {"logs-unavailable"})
        while pending:
            directory, depth = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > self.discovery_budget or len(files) >= self.max_files:
                            reasons.add("discovery-limit")
                            pending.clear()
                            break
                        path = Path(entry.path)
                        if entry.is_symlink() or path_is_reparse(entry.stat(follow_symlinks=False)):
                            reasons.add("unsafe-path")
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if path.name == PRE_TRAJECTORY_ARCHIVE:
                                continue
                            if depth >= 8:
                                reasons.add("discovery-limit")
                            else:
                                pending.append((path, depth + 1))
                        elif re.search(r"\.jsonl(?:\.\d+)?$", path.name):
                            metadata = entry.stat(follow_symlinks=False)
                            if not stat.S_ISREG(metadata.st_mode):
                                reasons.add("unsafe-path")
                                continue
                            files.append(_File(path, metadata.st_dev, metadata.st_ino,
                                               metadata.st_size, metadata.st_mtime_ns))
            except OSError:
                reasons.add("unreadable-file")
        files.sort(key=lambda item: item.path.relative_to(self.root).as_posix())
        return _Snapshot(time.monotonic(), files, reasons)

    def _open(self, item: _File):
        relative = item.path.relative_to(self.root)
        directory = self.root
        if directory.is_symlink() or path_is_reparse(directory.lstat()):
            raise OSError("unsafe root")
        for part in relative.parts[:-1]:
            directory = directory / part
            if directory.is_symlink() or path_is_reparse(directory.lstat()):
                raise OSError("unsafe parent")
        if item.path.is_symlink() or path_is_reparse(item.path.lstat()):
            raise OSError("unsafe file")
        if not item.path.resolve().is_relative_to(self.root.resolve()):
            raise OSError("outside root")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(item.path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != (item.device, item.inode):
                raise LogCursorInvalid("log snapshot changed")
            if metadata.st_size < item.size or (metadata.st_size == item.size and metadata.st_mtime_ns != item.mtime):
                raise LogCursorInvalid("log snapshot changed")
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise

    def _cursor(self, key: str, index: int, offset: int, dropping: bool) -> str:
        payload = f"{key}.{index}.{offset}.{int(dropping)}"
        digest = hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{digest}"

    def _resume(self, cursor: str) -> tuple[str, int, int, bool]:
        try:
            key, index, offset, dropping, digest = cursor.split(".")
            payload = f"{key}.{index}.{offset}.{dropping}"
            expected = hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(digest, expected) or dropping not in {"0", "1"}:
                raise ValueError
            return key, int(index), int(offset), dropping == "1"
        except (ValueError, TypeError):
            raise LogCursorInvalid("log cursor is invalid") from None

    def read(self, *, limit: int = 200, cursor: str | None = None) -> dict[str, Any]:
        """Return raw validated records; caller must apply its own safe DTO."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 256):
            raise LogCursorInvalid("log cursor is invalid")
        with self._lock:
            now = time.monotonic()
            self._snapshots = {key: value for key, value in self._snapshots.items()
                               if now - value.created < self.ttl}
            if cursor is None:
                while len(self._snapshots) >= 8:
                    del self._snapshots[next(iter(self._snapshots))]
                key = secrets.token_hex(12)
                snapshot = self._discover()
                self._snapshots[key] = snapshot
                index, offset, dropping = 0, 0, False
            else:
                key, index, offset, dropping = self._resume(cursor)
                snapshot = self._snapshots.get(key)
                if snapshot is None:
                    raise LogCursorInvalid("log snapshot expired")
                for item in snapshot.files:
                    try:
                        with self._open(item):
                            pass
                    except OSError:
                        raise LogCursorInvalid("log snapshot unavailable") from None
            return self._read(snapshot, key, index, offset, dropping, limit)

    def _read(self, snapshot: _Snapshot, key: str, index: int, offset: int,
              dropping: bool, limit: int) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        scanned = 0
        skipped = 0
        while index < len(snapshot.files) and len(rows) < limit and scanned < self.byte_budget:
            item = snapshot.files[index]
            try:
                with self._open(item) as stream:
                    stream.seek(offset)
                    while offset < item.size and len(rows) < limit and scanned < self.byte_budget:
                        maximum = min(self.line_bytes + 1, item.size - offset)
                        chunk = stream.readline(maximum)
                        if not chunk:
                            raise LogCursorInvalid("log snapshot truncated")
                        offset += len(chunk)
                        scanned += len(chunk)
                        ended = chunk.endswith(b"\n")
                        if dropping or not ended or len(chunk) > self.line_bytes:
                            if not dropping:
                                skipped += 1
                            dropping = not ended
                            snapshot.reasons.add("invalid-record")
                            continue
                        if not chunk.strip():
                            continue
                        try:
                            value = json.loads(chunk)
                        except (ValueError, UnicodeError, RecursionError):
                            value = None
                        if not _valid_entry(value):
                            skipped += 1
                            snapshot.reasons.add("invalid-record")
                        else:
                            rows.append(value)
            except OSError:
                snapshot.reasons.add("unreadable-file")
                offset = item.size
            if offset >= item.size:
                index += 1
                offset = 0
                dropping = False
        has_more = index < len(snapshot.files)
        if has_more and scanned >= self.byte_budget:
            snapshot.reasons.add("scan-limit")
        return {"entries": rows, "nextCursor": self._cursor(key, index, offset, dropping) if has_more else None,
                "partial": bool(snapshot.reasons), "partialReasons": sorted(snapshot.reasons),
                "skippedLines": skipped, "order": "file-offset", "snapshotExpiresInSeconds": max(0, int(self.ttl - (time.monotonic() - snapshot.created)))}
