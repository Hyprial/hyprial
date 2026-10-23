"""Private, bounded publication of an over-budget batch rendering.

The files are a cache of live inbox data, never authority: losing one costs a
cursor replay and a deterministic regeneration while the messages stay live.
Retention is bounded without a ledger: one 24-hour age sweep before every
render plus a 32-file cap after every publish, always retaining the file just
returned.  Unexpected symlinks or non-regular entries are ignored, never
followed and never deleted.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from hyprial.daemon.impl.inbox.batch.cursor import cursor_digest

__all__ = [
    "BATCH_DIRECTORY",
    "BATCH_FILE_PREFIX",
    "MAX_RETAINED_FILES",
    "RETENTION_SECONDS",
    "SpillFile",
    "SpillUnavailable",
    "prune",
    "publish",
    "spill_directory",
]

BATCH_DIRECTORY = ("state", "inbox-batches")
BATCH_FILE_PREFIX = "batch-"
BATCH_FILE_SUFFIX = ".md"
RETENTION_SECONDS = 24 * 60 * 60
MAX_RETAINED_FILES = 32


class SpillUnavailable(RuntimeError):
    """No private actor state root; never fall back to a shared directory."""


@dataclass(frozen=True, slots=True)
class SpillFile:
    path: Path
    size_bytes: int
    sha256: str


def _private_directory(directory: Path, *, create: bool) -> None:
    try:
        info = directory.lstat()
    except FileNotFoundError:
        if not create:
            raise SpillUnavailable("actor state root is unavailable") from None
        directory.mkdir(mode=0o700, parents=False)
        info = directory.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SpillUnavailable("actor state root is not a real directory")
    if info.st_uid != os.geteuid():
        raise SpillUnavailable("actor state root is not owned by the daemon user")
    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o700:
        if not create:
            raise SpillUnavailable("actor state root is not private")
        # A directory this daemon owns and just had to tighten: make it
        # private rather than silently writing a batch below a world-readable
        # path.
        os.chmod(directory, 0o700)


def spill_directory(actor_home: Path) -> Path:
    """``<actor-home>/state/inbox-batches``, verified private along the chain."""

    home = Path(actor_home)
    try:
        home_info = home.lstat()
    except FileNotFoundError:
        raise SpillUnavailable("actor home is unavailable") from None
    if stat.S_ISLNK(home_info.st_mode) or not stat.S_ISDIR(home_info.st_mode):
        raise SpillUnavailable("actor home is not a real directory")
    if home_info.st_uid != os.geteuid():
        raise SpillUnavailable("actor home is not owned by the daemon user")
    directory = home
    for part in BATCH_DIRECTORY:
        directory = directory / part
        _private_directory(directory, create=True)
    return directory


def _entries(directory: Path) -> list[tuple[Path, os.stat_result]]:
    found: list[tuple[Path, os.stat_result]] = []
    try:
        names = os.listdir(directory)
    except OSError:
        return found
    for name in names:
        if not (name.startswith(BATCH_FILE_PREFIX) and name.endswith(BATCH_FILE_SUFFIX)):
            continue
        candidate = directory / name
        try:
            info = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            # Never follow, never delete: an unexpected entry is not ours.
            continue
        found.append((candidate, info))
    return found


def prune(actor_home: Path, *, keep: Path | None = None, now: float | None = None) -> int:
    """Age sweep plus newest-32 cap for one actor; never settles any message."""

    try:
        directory = spill_directory(actor_home)
    except SpillUnavailable:
        return 0
    moment = time.time() if now is None else now
    removed = 0
    entries = _entries(directory)
    retained = [entry for entry in entries if entry[0] != keep]
    stale = [
        entry
        for entry in retained
        if moment - entry[1].st_mtime > RETENTION_SECONDS
    ]
    keep_paths = {entry[0] for entry in stale}
    for path, _info in stale:
        try:
            path.unlink()
            removed += 1
        except OSError:
            keep_paths.discard(path)
    survivors = sorted(
        (entry for entry in retained if entry[0] not in keep_paths),
        key=lambda entry: (entry[1].st_mtime, entry[0].name),
        reverse=True,
    )
    allowed = MAX_RETAINED_FILES - (1 if keep is not None else 0)
    for path, _info in survivors[max(allowed, 0):]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _target(directory: Path, cursor: str) -> Path:
    return directory / (
        f"{BATCH_FILE_PREFIX}{cursor_digest(cursor)[:32]}{BATCH_FILE_SUFFIX}"
    )


def publish(actor_home: Path, cursor: str, text: str) -> SpillFile:
    """Atomically publish the exact full rendering, deterministic per cursor."""

    directory = spill_directory(actor_home)
    payload = text.encode("utf-8")
    target = _target(directory, cursor)
    scratch = directory / (
        f"{BATCH_FILE_PREFIX}{secrets.token_hex(8)}.{os.getpid()}.tmp"
    )
    descriptor = os.open(
        scratch,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(scratch, 0o600)
        os.replace(scratch, target)
    except OSError as error:
        try:
            os.unlink(scratch)
        except OSError:
            pass
        raise SpillUnavailable(f"could not publish the batch spill file: {error}") from error
    info = target.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SpillUnavailable("batch spill file is not a regular file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        os.chmod(target, 0o600)
    prune(actor_home, keep=target)
    return SpillFile(
        path=target,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
