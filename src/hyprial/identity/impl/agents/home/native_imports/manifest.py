# Decision record (native_imports/manifest, W1E-1 2026-11-21):
# - Replaced POSIX-only dir_fd/O_NOFOLLOW/O_NONBLOCK capability gating with
#   portable Path.lstat() + manysys.path_is_reparse checks so import digests are
#   observed on Windows as well as POSIX instead of silently returning None.
# - Retained the constant 0o600 slot in the hashed row tuple so persisted
#   native_auth_imports manifest digests remain compatible across platforms.
"""Bounded digest observations; no credential bytes or digests leave inventory."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

from hyprial.kernel import manysys

MAX_FILE_BYTES = 1024 * 1024


def document_digest(documents) -> str | None:
    if any(not content or len(content) > MAX_FILE_BYTES for _, content in documents):
        return None
    return _digest([(name, len(content), hashlib.sha256(content).hexdigest(), 0o600)
                    for name, content in documents])


def observe(directory: Path | os.PathLike[str] | str, names) -> str | None:
    try:
        root = Path(directory)
        root_metadata = root.lstat()
    except (OSError, TypeError):
        return None
    if (
        stat.S_ISLNK(root_metadata.st_mode)
        or manysys.path_is_reparse(root_metadata)
        or not stat.S_ISDIR(root_metadata.st_mode)
    ):
        return None
    rows = []
    for name in sorted(names):
        for suffix in (".incoming", ".previous"):
            try:
                (root / (name + suffix)).lstat()
            except FileNotFoundError:
                pass
            except OSError:
                return None
            else:
                return None
        target = root / name
        try:
            before = target.lstat()
        except OSError:
            return None
        if (
            stat.S_ISLNK(before.st_mode)
            or manysys.path_is_reparse(before)
            or not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_size <= MAX_FILE_BYTES
        ):
            return None
        try:
            with target.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    return None
                digest = hashlib.sha256()
                count = 0
                while content := stream.read(min(65536, MAX_FILE_BYTES + 1 - count)):
                    count += len(content)
                    if count > MAX_FILE_BYTES:
                        return None
                    digest.update(content)
            after = target.lstat()
        except OSError:
            return None
        if (
            stat.S_ISLNK(after.st_mode)
            or manysys.path_is_reparse(after)
            or _identity(before) != _identity(after)
            or count != before.st_size
        ):
            return None
        rows.append((name, count, digest.hexdigest(), 0o600))
    return _digest(rows)


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mode,
        metadata.st_mtime_ns,
    )


def _digest(rows) -> str:
    encoded = json.dumps(sorted(rows), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
