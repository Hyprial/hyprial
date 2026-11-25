"""Registry-owned native import records and shared filesystem serialization.

The registry lock precedes the per-home lock, including default adoption.
Records contain digests only and bind the Agent incarnation and home resource.
A write receipt proves only local import. A separate response observation is
bound to that import generation and does not assert future vendor availability.
"""

from contextlib import contextmanager, nullcontext
from functools import wraps
from pathlib import Path
from threading import Lock, RLock
from weakref import WeakValueDictionary
from uuid import uuid4
import sqlite3

_LOCKS = WeakValueDictionary()
_LOCKS_GUARD = Lock()


def _valid_timestamp(value: object) -> bool:
    return type(value) is int and value > 0


def serialized(operation):
    @wraps(operation)
    def run(agent_home, harness, *args, **kwargs):
        record = kwargs.get("import_record")
        with record.guard(agent_home) if record is not None else nullcontext():
            key = (str(Path(agent_home).resolve()), harness)
            with _LOCKS_GUARD:
                lock = _LOCKS.get(key)
                if lock is None:
                    lock = RLock()
                    _LOCKS[key] = lock
            with lock:
                return operation(agent_home, harness, *args, **kwargs)
    return run


def initialize(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        _initialize(connection)
    else:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _initialize(connection)


def _initialize(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS native_auth_imports ("
        "actor TEXT NOT NULL, entity_token TEXT NOT NULL, harness TEXT NOT NULL, "
        "resource_token TEXT NOT NULL, manifest TEXT NOT NULL, imported_at_ms INTEGER NOT NULL, "
        "PRIMARY KEY(actor,harness), "
        "FOREIGN KEY(actor,entity_token) REFERENCES agents(actor,entity_token) ON DELETE CASCADE)"
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(native_auth_imports)")}
    if "import_id" not in columns:
        connection.execute("ALTER TABLE native_auth_imports ADD COLUMN import_id TEXT")
    if "confirmed_at_ms" not in columns:
        connection.execute("ALTER TABLE native_auth_imports ADD COLUMN confirmed_at_ms INTEGER")
    with nullcontext() if connection.in_transaction else connection:
        connection.execute("UPDATE native_auth_imports SET import_id=lower(hex(randomblob(16))) WHERE import_id IS NULL")


class NativeImportRecord:
    """A receipt port selected by the registry, not browser-supplied identity."""

    def __init__(self, registry, actor):
        self._registry = registry
        self._home = registry.home_receipt(actor, require_ready=False)

    @contextmanager
    def guard(self, agent_home):
        from ..native_credential import NativeCredentialError
        from hyprial.kernel import ipc_errors

        with self._registry._lock:
            current = self._registry.home_receipt(self._home.actor, require_ready=False)
            if (Path(agent_home) != Path(current.path) or current != self._home
                    or self._registry.require(current.actor).entity_token != current.entity_token):
                raise NativeCredentialError(ipc_errors.DAEMON_ERROR, "native import Agent incarnation changed")
            try:
                yield
            except sqlite3.Error as error:
                raise NativeCredentialError(ipc_errors.DAEMON_ERROR, "native import record unavailable") from error

    def read(self, harness: str, manifest: str | None) -> int | None:
        if manifest is None:
            return None
        row = self._registry._db.execute(
            "SELECT imported_at_ms FROM native_auth_imports "
            "WHERE actor=? AND entity_token=? AND resource_token=? AND harness=? AND manifest=?",
            (self._home.actor, self._home.entity_token, self._home.resource_token, harness, manifest),
        ).fetchone()
        return row[0] if row is not None and _valid_timestamp(row[0]) else None

    def exists(self, harness: str) -> bool:
        return self._registry._db.execute(
            "SELECT 1 FROM native_auth_imports WHERE actor=? AND entity_token=? "
            "AND resource_token=? AND harness=?",
            (self._home.actor, self._home.entity_token, self._home.resource_token, harness),
        ).fetchone() is not None

    def snapshot(self, harness: str, manifest: str | None) -> tuple[str, str] | None:
        if manifest is None:
            return None
        row = self._registry._db.execute(
            "SELECT import_id,imported_at_ms FROM native_auth_imports "
            "WHERE actor=? AND entity_token=? AND resource_token=? AND harness=? AND manifest=?",
            (self._home.actor, self._home.entity_token, self._home.resource_token, harness, manifest),
        ).fetchone()
        return (str(row[0]), manifest) if row is not None and _valid_timestamp(row[1]) else None

    def confirmed_at(self, harness: str, manifest: str | None) -> int | None:
        if manifest is None:
            return None
        row = self._registry._db.execute(
            "SELECT confirmed_at_ms,imported_at_ms FROM native_auth_imports "
            "WHERE actor=? AND entity_token=? AND resource_token=? AND harness=? AND manifest=?",
            (self._home.actor, self._home.entity_token, self._home.resource_token, harness, manifest),
        ).fetchone()
        return row[0] if row is not None and _valid_timestamp(row[0]) and _valid_timestamp(row[1]) else None

    def confirm(self, harness: str, snapshot: tuple[str, str], at_ms: int) -> None:
        if not _valid_timestamp(at_ms):
            raise ValueError("native confirmation timestamp must be a positive integer")
        self._execute(
            "UPDATE native_auth_imports SET confirmed_at_ms=max(coalesce(confirmed_at_ms,0),?) "
            "WHERE actor=? AND entity_token=? AND resource_token=? AND harness=? AND import_id=? AND manifest=?",
            (at_ms, self._home.actor, self._home.entity_token, self._home.resource_token, harness, *snapshot),
        )

    def write(self, harness: str, manifest: str, at_ms: int | None = None) -> None:
        at_ms = self._registry.now_ms() if at_ms is None else at_ms
        if not _valid_timestamp(at_ms):
            raise ValueError("native import timestamp must be a positive integer")
        self._execute(
            "INSERT OR REPLACE INTO native_auth_imports "
            "(actor,entity_token,harness,resource_token,manifest,imported_at_ms,import_id) VALUES(?,?,?,?,?,?,?)",
            (self._home.actor, self._home.entity_token, harness, self._home.resource_token, manifest, at_ms, uuid4().hex),
        )

    def clear(self, harness: str) -> None:
        self._execute("DELETE FROM native_auth_imports WHERE actor=? AND harness=?", (self._home.actor, harness))

    def restore(self, harness: str, snapshot: tuple[str, str], at_ms: int, confirmed_at_ms: int | None) -> None:
        """Restore a writer checkpoint only after the old file snapshot is restored."""
        if not _valid_timestamp(at_ms) or (confirmed_at_ms is not None and not _valid_timestamp(confirmed_at_ms)):
            raise ValueError("native import checkpoint timestamps must be positive integers")
        self._execute(
            "INSERT OR REPLACE INTO native_auth_imports "
            "(actor,entity_token,harness,resource_token,manifest,imported_at_ms,import_id,confirmed_at_ms) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (self._home.actor, self._home.entity_token, harness, self._home.resource_token,
             snapshot[1], at_ms, snapshot[0], confirmed_at_ms),
        )

    def _execute(self, sql, parameters):
        connection = self._registry._db
        with nullcontext() if connection.in_transaction else connection:
            connection.execute(sql, parameters)
