"""Local-override persistence, kept separate from read-only legacy rows."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class OverrideRow:
    platform: str
    union_id: str
    user_key: str
    confirmed_by: str
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class AccountOverrideRow:
    """An override set by {adapter, openId}, resolving through its unionId.

    Kept apart from union-keyed rows so each clear removes exactly what its
    own set created (§5 symmetry).
    """

    adapter: str
    open_id: str
    union_id: str
    user_key: str
    confirmed_by: str
    updated_at_ms: int


class OverrideConflictError(RuntimeError):
    """A union-id override must be cleared before it can move users."""


class OverrideStore:
    """The only R1 writer for union-id overrides."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(self.path, timeout=5.0)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA busy_timeout=5000")
        database.execute(
            """CREATE TABLE IF NOT EXISTS identity_overrides (
                   platform TEXT NOT NULL,
                   union_id TEXT NOT NULL,
                   user_key TEXT NOT NULL,
                   confirmed_by TEXT NOT NULL,
                   updated_at_ms INTEGER NOT NULL,
                   PRIMARY KEY(platform, union_id)
               )"""
        )
        database.execute(
            """CREATE TABLE IF NOT EXISTS identity_account_overrides (
                   adapter TEXT NOT NULL,
                   open_id TEXT NOT NULL,
                   union_id TEXT NOT NULL,
                   user_key TEXT NOT NULL,
                   confirmed_by TEXT NOT NULL,
                   updated_at_ms INTEGER NOT NULL,
                   PRIMARY KEY(adapter, open_id)
               )"""
        )
        return database

    @staticmethod
    def _row(value: sqlite3.Row) -> OverrideRow:
        return OverrideRow(
            platform=value["platform"],
            union_id=value["union_id"],
            user_key=value["user_key"],
            confirmed_by=value["confirmed_by"],
            updated_at_ms=value["updated_at_ms"],
        )

    def list(self, *, platform: str | None = None) -> tuple[OverrideRow, ...]:
        if not self.path.is_file():
            return ()
        with self._lock, closing(self._connect()) as database:
            if platform is None:
                rows = database.execute(
                    "SELECT * FROM identity_overrides"
                    " ORDER BY platform, union_id"
                ).fetchall()
            else:
                rows = database.execute(
                    "SELECT * FROM identity_overrides WHERE platform = ?"
                    " ORDER BY union_id",
                    (platform,),
                ).fetchall()
        return tuple(self._row(row) for row in rows)

    def set(
        self,
        *,
        platform: str,
        union_id: str,
        user_key: str,
        confirmed_by: str,
        now_ms: int | None = None,
    ) -> OverrideRow:
        updated = int(time.time() * 1000) if now_ms is None else now_ms
        with self._lock, closing(self._connect()) as database, database:
            existing = database.execute(
                "SELECT * FROM identity_overrides"
                " WHERE platform = ? AND union_id = ?",
                (platform, union_id),
            ).fetchone()
            if existing is not None:
                row = self._row(existing)
                if row.user_key != user_key:
                    raise OverrideConflictError(
                        f"{platform} unionId {union_id!r} is overridden to"
                        f" user {row.user_key!r}; clear it first"
                    )
                return row
            database.execute(
                """INSERT INTO identity_overrides(
                       platform, union_id, user_key, confirmed_by, updated_at_ms
                   ) VALUES (?, ?, ?, ?, ?)""",
                (platform, union_id, user_key, confirmed_by, updated),
            )
            row = database.execute(
                "SELECT * FROM identity_overrides"
                " WHERE platform = ? AND union_id = ?",
                (platform, union_id),
            ).fetchone()
        assert row is not None
        return self._row(row)

    def clear(self, *, platform: str, union_id: str) -> bool:
        if not self.path.is_file():
            return False
        with self._lock, closing(self._connect()) as database, database:
            cursor = database.execute(
                "DELETE FROM identity_overrides"
                " WHERE platform = ? AND union_id = ?",
                (platform, union_id),
            )
        return cursor.rowcount > 0


    def list_accounts(self) -> tuple[AccountOverrideRow, ...]:
        if not self.path.is_file():
            return ()
        with self._lock, closing(self._connect()) as database:
            rows = database.execute(
                "SELECT * FROM identity_account_overrides ORDER BY adapter, open_id"
            ).fetchall()
        return tuple(AccountOverrideRow(**dict(row)) for row in rows)

    def set_account(
        self,
        *,
        adapter: str,
        open_id: str,
        union_id: str,
        user_key: str,
        confirmed_by: str,
        now_ms: int | None = None,
    ) -> AccountOverrideRow:
        updated = int(time.time() * 1000) if now_ms is None else now_ms
        with self._lock, closing(self._connect()) as database, database:
            database.execute(
                """INSERT INTO identity_account_overrides(
                       adapter, open_id, union_id, user_key, confirmed_by,
                       updated_at_ms
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(adapter, open_id) DO NOTHING""",
                (adapter, open_id, union_id, user_key, confirmed_by, updated),
            )
            row = database.execute(
                "SELECT * FROM identity_account_overrides"
                " WHERE adapter = ? AND open_id = ?",
                (adapter, open_id),
            ).fetchone()
        assert row is not None
        return AccountOverrideRow(**dict(row))

    def clear_account(self, *, adapter: str, open_id: str) -> bool:
        if not self.path.is_file():
            return False
        with self._lock, closing(self._connect()) as database, database:
            cursor = database.execute(
                "DELETE FROM identity_account_overrides"
                " WHERE adapter = ? AND open_id = ?",
                (adapter, open_id),
            )
        return cursor.rowcount > 0


class GuardedOverrideStore:
    """An OverrideStore whose source failures surface as one caller error.

    A locked or unreadable identity.sqlite3 raises sqlite3/OS errors from
    every method; the resolver's other sources already report those as
    "source unavailable", so this one does too (review 1023).
    """

    def __init__(
        self,
        store: OverrideStore,
        unavailable: Callable[[Exception], Exception],
    ) -> None:
        self._store = store
        self._unavailable = unavailable

    def _guarded(self, method: str, **params: object) -> Any:
        try:
            return getattr(self._store, method)(**params)
        except (sqlite3.Error, OSError) as error:
            raise self._unavailable(error) from error

    def list(self, **params: object) -> tuple[OverrideRow, ...]:
        return self._guarded("list", **params)

    def set(self, **params: object) -> OverrideRow:
        return self._guarded("set", **params)

    def clear(self, **params: object) -> bool:
        return self._guarded("clear", **params)

    def list_accounts(self) -> tuple[AccountOverrideRow, ...]:
        return self._guarded("list_accounts")

    def set_account(self, **params: object) -> AccountOverrideRow:
        return self._guarded("set_account", **params)

    def clear_account(self, **params: object) -> bool:
        return self._guarded("clear_account", **params)
