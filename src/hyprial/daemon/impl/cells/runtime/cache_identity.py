"""Read-only identity check of the local cell cache (contract OC0 §1.9 item 7).

Decision notes (cell model OC1a):

- One implementation for the composition check before ``CellCache`` opens
  the file, for a later doctor check, and for the login line if it wants it.
- It never creates, writes or locks anything: the main file is opened as
  ``file:<path>?mode=ro&immutable=1`` and only ``SELECT key, value FROM
  cache_meta`` runs.  The identity keys are written once, inside the main
  file, by ``create_cell_cache``, so ignoring the WAL cannot misread them.
- ``"absent"``: none of ``cells.sqlite3``, ``-wal``, ``-shm``, ``.new`` exists.
  None: something exists but the three keys cannot be read (a cache of an
  earlier build, an empty database, an unreadable file, or a
  ``-wal``/``-shm``/``.new`` without the main file).
- ``cell_cache_identity_conflict`` returns a structured ``CellCacheConflict``
  with ``reason`` (``cache-unreadable`` when any ``.new`` staging file exists,
  when the main file is missing beside a ``-wal``/``-shm`` or any
  ``sqlite3.Error``/``OSError`` occurs;
  otherwise ``cache-identity-mismatch``) and ``keys`` naming every key that is
  missing (an earlier build's cache, which only has ``author``; an empty
  database) or differs, key names only, never values.  A None ``writer``
  skips the writer comparison.  It never raises.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from hyprial.daemon.impl.cells.runtime.cache import CACHE_FILENAME, IDENTITY_KEYS, NEW_SUFFIX

UNREADABLE: Literal["cache-unreadable"] = "cache-unreadable"
MISMATCH: Literal["cache-identity-mismatch"] = "cache-identity-mismatch"


@dataclass(frozen=True, slots=True)
class CellCacheIdentity:
    """The identity a cell cache was created for."""

    authority: str
    issuer: str
    writer: str


@dataclass(frozen=True, slots=True)
class CellCacheConflict:
    """Why an existing cell cache cannot be opened for the current identity."""

    reason: Literal["cache-unreadable", "cache-identity-mismatch"]
    keys: tuple[str, ...] = ()

    @property
    def keys_csv(self) -> str:
        return ",".join(self.keys)

    def __str__(self) -> str:
        return f"{self.reason}:{self.keys_csv}" if self.keys else self.reason


def cache_files(state_dir: Path) -> tuple[Path, Path, Path]:
    """The cache file and its WAL and shared-memory companions."""

    main = Path(state_dir) / CACHE_FILENAME
    return main, main.with_name(main.name + "-wal"), main.with_name(main.name + "-shm")


def _recorded(state_dir: Path) -> dict[str, object] | Literal["absent", "unreadable"]:
    try:
        main, wal, shm = cache_files(state_dir)
        staging = main.with_name(main.name + NEW_SUFFIX)
        present = [path.exists() or path.is_symlink() for path in (main, wal, shm, staging)]
    except OSError:
        return "unreadable"
    if not any(present):
        return "absent"
    if present[3]:
        return "unreadable"
    if not present[0]:
        return "unreadable"
    try:
        connection = sqlite3.connect("file:" + quote(str(main)) + "?mode=ro&immutable=1", uri=True)
        try:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'cache_meta'"
            ).fetchall()
            rows = (
                connection.execute("SELECT key, value FROM cache_meta").fetchall() if tables else []
            )
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        return "unreadable"
    return {str(key): value for key, value in rows}


def read_cell_cache_identity(state_dir: Path) -> CellCacheIdentity | None | Literal["absent"]:
    """The recorded identity, None when unreadable or incomplete, ``"absent"`` when no file."""

    recorded = _recorded(state_dir)
    if recorded == "absent":
        return "absent"
    if recorded == "unreadable":
        return None
    values = [recorded.get(key) for key in IDENTITY_KEYS]
    if not all(isinstance(value, str) for value in values):
        return None
    authority, issuer, writer = (str(value) for value in values)
    return CellCacheIdentity(authority, issuer, writer)


def cell_cache_identity_conflict(
    state_dir: Path, *, authority: str, issuer: str, writer: str | None = None
) -> CellCacheConflict | None:
    """None when no cache exists or it matches; otherwise the structured refusal."""

    try:
        recorded = _recorded(state_dir)
    except Exception:  # noqa: BLE001 - a read-only probe never raises
        return CellCacheConflict(UNREADABLE)
    if recorded == "absent":
        return None
    if recorded == "unreadable":
        return CellCacheConflict(UNREADABLE)
    current = {"authority": authority, "issuer": issuer, "writer": writer}
    mismatched = tuple(
        key
        for key in IDENTITY_KEYS
        if not isinstance(recorded.get(key), str)
        or (current[key] is not None and recorded[key] != current[key])
    )
    return CellCacheConflict(MISMATCH, mismatched) if mismatched else None


__all__ = [
    "CellCacheConflict",
    "CellCacheIdentity",
    "cache_files",
    "cell_cache_identity_conflict",
    "read_cell_cache_identity",
]
