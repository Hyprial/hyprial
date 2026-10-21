"""Storage primitives for the durable mobile-access authority.

One SQLite file, one cross-process advisory lock, one durable state-sequence
epoch.  This module owns the schema, the transaction boundary, clock rollback
detection and the append-only audit table; the state-machine methods live in
:mod:`store` and :mod:`replay`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hyprial.kernel import lock_exclusive, unlock
from hyprial.shell.impl.gui.adapter.mobile.models import (
    CLOCK_REGRESSION_SECONDS,
    MobileAccessError,
    _identifier,
    _refusal,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pairing_codes (
  code_hash TEXT PRIMARY KEY,
  salt TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  consumed_at REAL,
  active INTEGER NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pairing_requests (
  request_id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  device_uid TEXT NOT NULL,
  name TEXT NOT NULL,
  owner TEXT NOT NULL,
  client_public TEXT NOT NULL,
  permissions TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  node_id TEXT NOT NULL,
  requested_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  status TEXT NOT NULL,
  approved_at REAL,
  approved_by TEXT,
  rejected_at REAL,
  rejected_by TEXT,
  pairing_attempt_hash TEXT,
  code_hash TEXT
);
CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY,
  device_uid TEXT NOT NULL,
  name TEXT NOT NULL,
  owner TEXT NOT NULL,
  client_public TEXT NOT NULL,
  key_generation INTEGER NOT NULL,
  status TEXT NOT NULL,
  permissions TEXT NOT NULL,
  approved_at REAL NOT NULL,
  revoked_at REAL
);
CREATE TABLE IF NOT EXISTS device_generations (
  device_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  client_public TEXT NOT NULL,
  request_id TEXT NOT NULL,
  approved_at REAL NOT NULL,
  revoked_at REAL,
  PRIMARY KEY (device_id, generation)
);
CREATE TABLE IF NOT EXISTS session_grants (
  device_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  owner TEXT NOT NULL,
  granted_at REAL NOT NULL,
  granted_by TEXT NOT NULL,
  shared_from TEXT,
  revoked_at REAL,
  PRIMARY KEY (device_id, session_id)
);
CREATE TABLE IF NOT EXISTS nonces (
  device_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  nonce TEXT NOT NULL,
  expires_at REAL NOT NULL,
  consumed_at REAL NOT NULL,
  PRIMARY KEY (device_id, generation, nonce)
);
CREATE TABLE IF NOT EXISTS access_audit (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL,
  event TEXT NOT NULL,
  device_id TEXT,
  session_id TEXT,
  operation TEXT,
  sharing TEXT,
  source_device_id TEXT,
  status TEXT,
  detail TEXT
);
"""

#: Additive columns introduced by the pairing-attempt slice.  Existing
#: databases keep their rows; new requests carry a hashed attempt binding.
_PAIRING_REQUEST_COLUMNS = (
    ("pairing_attempt_hash", "TEXT"),
    ("code_hash", "TEXT"),
)


def _migrate(connection: sqlite3.Connection) -> None:
    """Bring an existing database forward without rewriting old rows.

    ``CREATE TABLE IF NOT EXISTS`` does not add columns to a table that was
    created by an older release, so the additive columns are applied here.
    Both are nullable: pre-attempt requests stay readable, and the partial
    unique index only constrains attempt-aware requests.
    """

    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(pairing_requests)")
    }
    for name, kind in _PAIRING_REQUEST_COLUMNS:
        if name not in columns:
            connection.execute(
                f"ALTER TABLE pairing_requests ADD COLUMN {name} {kind}"
            )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS pairing_request_attempt_hash_unique "
        "ON pairing_requests(pairing_attempt_hash) "
        "WHERE pairing_attempt_hash IS NOT NULL"
    )


class _Core:
    """Open/transaction/epoch/audit primitives shared by the state machine."""

    def __init__(
        self,
        path: Path | str,
        *,
        node_id: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        self.node_id = _identifier(node_id, "nodeId")
        self.clock = clock
        self._epoch_path = Path(f"{self._path}.epoch")
        self._lock_path = Path(f"{self._path}.lock")
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._open()

    @property
    def path(self) -> Path:
        return self._path

    # -- lifecycle ----------------------------------------------------------

    def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self._path), timeout=10.0, check_same_thread=False
        )
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")  # durability: FULL -- revocation t0 and nonce commits must survive an OS crash
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            _migrate(connection)
            stored_node = self._read_meta(connection, "node_id")
            if stored_node is None:
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES('node_id', ?)", (self.node_id,)
                )
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES('state_seq', '0')"
                )
                connection.execute("COMMIT")
                self._write_epoch(0)
            else:
                if stored_node != self.node_id:
                    raise _refusal(
                        "node-identity-mismatch",
                        "the access database belongs to another host node id",
                        "host-node-mismatch",
                    )
                sequence = int(self._read_meta(connection, "state_seq") or "0")
                connection.execute("COMMIT")
                self._check_epoch(sequence)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()
            raise
        os.chmod(self._path, 0o600)
        self._conn = connection

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> _Core:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise _refusal(
                "access-store-unavailable",
                "the durable access authority is closed",
                "storage-unavailable",
            )
        return self._conn

    def _check_epoch(self, sequence: int) -> None:
        try:
            raw = self._epoch_path.read_text(encoding="ascii").strip()
        except FileNotFoundError as error:
            raise _refusal(
                "access-state-unavailable",
                "the access authority's epoch file is missing",
                "storage-unavailable",
            ) from error
        except OSError as error:
            raise _refusal(
                "access-store-unavailable",
                "the access authority's epoch file cannot be read",
                "storage-unavailable",
            ) from error
        if raw != str(sequence):
            raise _refusal(
                "access-state-rolled-back",
                "the access authority database is older than its durable epoch",
                "storage-rollback",
                databaseEpoch=sequence,
                durableEpoch=raw,
            )

    def _write_epoch(self, sequence: int) -> None:
        staging = self._epoch_path.with_name(f".{self._epoch_path.name}.tmp")
        try:
            with open(staging, "w", encoding="ascii") as handle:
                handle.write(str(sequence))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(staging, 0o600)
            os.replace(staging, self._epoch_path)
        except OSError as error:
            raise _refusal(
                "access-store-unavailable",
                "the access authority's epoch cannot be persisted",
                "storage-unavailable",
            ) from error

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        try:
            descriptor = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as error:
            raise _refusal(
                "access-store-unavailable",
                "the access authority lock cannot be opened",
                "storage-unavailable",
            ) from error
        try:
            try:
                lock_exclusive(descriptor)
            except OSError as error:
                raise _refusal(
                    "access-store-unavailable",
                    "the access authority lock cannot be acquired",
                    "storage-unavailable",
                ) from error
            try:
                yield
            finally:
                unlock(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._file_lock():
            connection = self._connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                sequence = self._bump_sequence(connection)
                connection.execute("COMMIT")
            except BaseException as error:
                if not connection.in_transaction:
                    raise
                if isinstance(error, MobileAccessError) and error.commit_state:
                    # Failures that maintain bounded attempt counters must
                    # persist even though the call itself is refused.
                    sequence = self._bump_sequence(connection)
                    connection.execute("COMMIT")
                    self._write_epoch(sequence)
                    raise
                connection.execute("ROLLBACK")
                raise
            self._write_epoch(sequence)

    def _read_meta(self, connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def _set_meta(self, connection: sqlite3.Connection, key: str, value: str) -> None:
        connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _bump_sequence(self, connection: sqlite3.Connection) -> int:
        sequence = int(self._read_meta(connection, "state_seq") or "0") + 1
        self._set_meta(connection, "state_seq", str(sequence))
        return sequence

    def _checked_now(
        self, connection: sqlite3.Connection, now: float | None
    ) -> float:
        value = float(self.clock() if now is None else now)
        last_raw = self._read_meta(connection, "last_now")
        last = float(last_raw) if last_raw is not None else 0.0
        if last and value < last - CLOCK_REGRESSION_SECONDS:
            raise _refusal(
                "auth-clock-anomaly",
                "the authorization clock moved backwards; refusing to widen a window",
                "clock-anomaly",
                now=value,
                lastObserved=last,
            )
        if value > last:
            self._set_meta(connection, "last_now", repr(value))
        return value

    # -- audit --------------------------------------------------------------

    def _audit(
        self,
        connection: sqlite3.Connection,
        at: float,
        event: str,
        *,
        device_id: str | None = None,
        session_id: str | None = None,
        operation: str | None = None,
        sharing: str | None = None,
        source_device_id: str | None = None,
        status: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO access_audit(at, event, device_id, session_id, operation, "
            "sharing, source_device_id, status, detail) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                at,
                event,
                device_id,
                session_id,
                operation,
                sharing,
                source_device_id,
                status,
                None if detail is None else json.dumps(detail, sort_keys=True),
            ),
        )

    def audit_events(
        self,
        *,
        device_id: str | None = None,
        session_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[object] = []
        if device_id is not None:
            clauses.append("device_id = ?")
            parameters.append(device_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            parameters.append(session_id)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        parameters.append(limit)
        with self._lock:
            rows = self._connection().execute(
                "SELECT audit_id, at, event, device_id, session_id, operation, "
                "sharing, source_device_id, status, detail FROM access_audit "
                f"{where}ORDER BY audit_id DESC LIMIT ?",
                tuple(parameters),
            ).fetchall()
        return [
            {
                "auditId": row[0],
                "at": row[1],
                "event": row[2],
                "deviceId": row[3],
                "sessionId": row[4],
                "operation": row[5],
                "sharing": row[6],
                "sourceDeviceId": row[7],
                "status": row[8],
                "detail": None if row[9] is None else json.loads(row[9]),
            }
            for row in rows
        ]

    def state_sequence(self) -> int:
        with self._lock:
            return int(self._read_meta(self._connection(), "state_seq") or "0")
