"""Local cell cache: ``<state>/cells.sqlite3`` (plan §3, S3).

Decision notes (phase A, S3):

- The cache is the local authority for lookups: the runtime loads it at
  start and serves every read from memory, so a daemon whose OrgFS is
  unreachable still answers lookups with the last cells it knew.
- A locally authored change is committed here with ``dirty=1`` before any
  OrgFS write.  ``mark_clean`` is a compare-and-set on the revision, so a
  newer local change made while an older one was being written stays dirty.
- ``cell_copies`` keeps the last known OrgFS node and version per (cell,
  space): it is the ``expect_version`` of the next write there.
- The file is not in ``MIGRATION_DATABASES``.  It belongs to one identity
  (contract OC0 §1.9): ``cache_meta`` holds ``authority``, ``issuer`` and
  ``writer``, written once by ``create_cell_cache`` and never updated.
  ``create_cell_cache`` builds the file as ``cells.sqlite3.new`` in rollback
  journal mode, writes the schema and the three keys in one transaction,
  fsyncs and renames it into place, so a crash leaves either no file or a
  complete one, and the keys live in the main file only.  It claims the
  staging path exclusively (``O_CREAT | O_EXCL``), failing closed if a leftover
  ``.new`` file already exists so nothing deletes an unknown staging file,
  cleaning up only the staging file created by the current attempt on failure
  before rename, and flushing the parent directory via
  ``hyprial.kernel.fsync_directory``.
- There is no owner-change re-mint, no alias table and no ``author`` key any more
  (owner ruling 2026-11-21): a cache of another identity, or of an earlier
  build, is refused before this class opens it (``cache_identity``), and
  nothing in the code deletes, rewrites or migrates it.
- A row that no longer decodes is skipped and counted, never fatal.
- WAL with ``synchronous = NORMAL`` (repository durability policy): a cell
  committed just before a power loss may roll back; the OrgFS copy or the
  caller's next ``ensure`` restores it.
- I6-5a: ``read_cached_cell`` is the launch path's read of one cell.  It
  opens the file read-only (no schema, no WAL pragma, no write), so a launch
  reads the local document whether or not the runtime is composed and never
  reaches OrgFS.  A missing or unreadable file raises; the caller decides.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from urllib.parse import quote

from hyprial.daemon.impl.cells.codec import decode, encode
from hyprial.daemon.impl.cells.model import Cell, CellError
from hyprial.kernel import fsync_directory

CACHE_FILENAME = "cells.sqlite3"
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS cells ("
    " cell_id TEXT PRIMARY KEY,"
    " kind TEXT NOT NULL,"
    " authority TEXT NOT NULL,"
    " revision INTEGER NOT NULL,"
    " dirty INTEGER NOT NULL,"
    " document TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS cell_copies ("
    " cell_id TEXT NOT NULL,"
    " space_id TEXT NOT NULL,"
    " node_id TEXT NOT NULL,"
    " version TEXT NOT NULL,"
    " PRIMARY KEY (cell_id, space_id))",
    "CREATE TABLE IF NOT EXISTS cache_meta ("
    " key TEXT PRIMARY KEY,"
    " value TEXT NOT NULL)",
)
IDENTITY_KEYS = ("authority", "issuer", "writer")
_UPSERT_CELL = (
    "INSERT INTO cells (cell_id, kind, authority, revision, dirty, document)"
    " VALUES (?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(cell_id) DO UPDATE SET kind = excluded.kind,"
    " authority = excluded.authority, revision = excluded.revision,"
    " dirty = excluded.dirty, document = excluded.document"
)
NEW_SUFFIX = ".new"


class CellCache:
    """SQLite-backed store of cells, dirty flags and OrgFS copy versions."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        for statement in _SCHEMA:
            self._connection.execute(statement)
        self.skipped_rows = 0

    def load(self) -> dict[str, tuple[Cell, bool]]:
        """Every decodable cell with its dirty flag."""

        with self._lock:
            rows = self._connection.execute(
                "SELECT cell_id, document, dirty FROM cells ORDER BY cell_id"
            ).fetchall()
        loaded: dict[str, tuple[Cell, bool]] = {}
        skipped = 0
        for cell_id, document, dirty in rows:
            try:
                cell = decode(document)
            except CellError:
                skipped += 1
                continue
            if cell.cell_id != cell_id:
                skipped += 1
                continue
            loaded[cell_id] = (cell, bool(dirty))
        self.skipped_rows = skipped
        return loaded

    def put(self, cell: Cell, *, dirty: bool) -> None:
        document = encode(cell)
        with self._lock:
            self._connection.execute(
                _UPSERT_CELL,
                (cell.cell_id, cell.kind, cell.authority, cell.revision, int(dirty), document),
            )

    def mark_clean(self, cell_id: str, revision: int) -> bool:
        """Clear the dirty flag only if *revision* is still the cached one."""

        with self._lock:
            cursor = self._connection.execute(
                "UPDATE cells SET dirty = 0 WHERE cell_id = ? AND revision = ?",
                (cell_id, revision),
            )
        return cursor.rowcount == 1

    def copies(self, cell_id: str) -> dict[str, tuple[str, str]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT space_id, node_id, version FROM cell_copies WHERE cell_id = ?",
                (cell_id,),
            ).fetchall()
        return {space: (node, version) for space, node, version in rows}

    def set_copy(self, cell_id: str, space_id: str, node_id: str, version: str) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO cell_copies (cell_id, space_id, node_id, version)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(cell_id, space_id) DO UPDATE"
                " SET node_id = excluded.node_id, version = excluded.version",
                (cell_id, space_id, node_id, version),
            )

    def drop_copy(self, cell_id: str, space_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM cell_copies WHERE cell_id = ? AND space_id = ?",
                (cell_id, space_id),
            )

    def drop_copies(self, cell_id: str) -> None:
        with self._lock:
            self._connection.execute("DELETE FROM cell_copies WHERE cell_id = ?", (cell_id,))

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def read_cached_cell(path: Path, cell_id: str) -> Cell | None:
    """The cached document of *cell_id*, or None when the cache holds none.

    Raises ``FileNotFoundError`` for a missing cache and ``sqlite3.Error`` or
    ``CellError`` for one that cannot be read.
    """

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    connection = sqlite3.connect(
        "file:" + quote(str(path)) + "?mode=ro", uri=True, isolation_level=None
    )
    try:
        connection.execute("PRAGMA busy_timeout = 1000")
        row = connection.execute(
            "SELECT document FROM cells WHERE cell_id = ?", (cell_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    cell = decode(row[0])
    if cell.cell_id != cell_id:
        raise CellError("invalid-cell", "cached cell id does not match its row")
    return cell


def create_cell_cache(path: Path, *, authority: str, issuer: str, writer: str) -> None:
    """Create a new cache file at *path* recording one identity, atomically.

    Only for a path where the file, its ``-wal``/``-shm`` and ``.new`` do not
    exist; the caller checks.  Raises ``FileExistsError`` rather than replace
    an existing cache or a leftover staging file, and unlinks only the staging
    file created by this attempt if creation fails before rename.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + NEW_SUFFIX)
    descriptor = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    renamed = False
    try:
        connection = sqlite3.connect(str(staging), isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.executemany(
                "INSERT INTO cache_meta (key, value) VALUES (?, ?)",
                zip(IDENTITY_KEYS, (authority, issuer, writer), strict=True),
            )
            connection.execute("COMMIT")
        finally:
            connection.close()
        with open(staging, "rb") as stream:
            os.fsync(stream.fileno())
        if path.exists():
            raise FileExistsError(str(path))
        os.rename(staging, path)
        renamed = True
        fsync_directory(path.parent)
    except BaseException:
        if not renamed:
            staging.unlink(missing_ok=True)
        raise


__all__ = [
    "CACHE_FILENAME",
    "IDENTITY_KEYS",
    "NEW_SUFFIX",
    "CellCache",
    "create_cell_cache",
    "read_cached_cell",
]
