from __future__ import annotations

from typing import Any
from collections.abc import Iterator
from pathlib import Path
from contextlib import contextmanager
from hyprial.identity.impl.users.home import ensure_user_home
from hyprial.kernel import ipc_errors, is_identity_id_segment
import json
import sqlite3
import threading
import time
from uuid import uuid4

from ._base import (
    USER_EXISTS,
    USER_KINDS,
    USER_NOT_FOUND,
    USER_OWNER_FORBIDDEN,
    USER_OWNER_REQUIRED,
    USER_SCHEMA_VERSION,
    User,
    UserEvent,
    UserStoreError,
    _GUEST_PREFIX,
    _SQLITE_BUSY_MS,
    _camel,
    _event,
    _now_ms,
    _owner_key,
    _required,
    _text,
    _user,
)

class UserStore():
    """SQLite at ``state_dir/users.sqlite3``; the authority for users.

    ``hyprial_home`` is optional: a writer that passes it keeps each user's
    home mirror current; a reader without it never creates directories under H.
    """
    def __init__(self, path: Path, *, hyprial_home: Path | None = None) -> None:
        self.path = path
        self._hyprial_home = hyprial_home
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(
            path,
            timeout=_SQLITE_BUSY_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            self._initialize()
        except BaseException:
            self._db.close()
            raise
    def close(self) -> None:
        with self._lock:
            self._db.close()
    def _initialize(self) -> None:
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_MS}")
        self._switch_to_wal()
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._db.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    user_key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('member', 'guest')),
                    owner TEXT,
                    nickname TEXT,
                    real_name TEXT,
                    display_name TEXT,
                    entity_token TEXT UNIQUE NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    -- A member IS an owner's person; a guest is nobody's
                    -- authorisation.  Enforced here as well as in code.
                    CHECK (
                        (kind = 'member' AND owner IS NOT NULL AND owner <> '')
                        OR (kind = 'guest' AND owner IS NULL)
                    )
                );

                CREATE TABLE IF NOT EXISTS user_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_key TEXT NOT NULL,
                    event TEXT NOT NULL,
                    actor TEXT NOT NULL CHECK (actor <> ''),
                    at_ms INTEGER NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS user_events_by_user
                    ON user_events(user_key, id);
                -- The audit trail is only worth anything if it cannot be
                -- rewritten after the fact.
                CREATE TRIGGER IF NOT EXISTS user_events_no_update
                    BEFORE UPDATE ON user_events
                    BEGIN SELECT RAISE(ABORT, 'user_events is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS user_events_no_delete
                    BEFORE DELETE ON user_events
                    BEGIN SELECT RAISE(ABORT, 'user_events is append-only'); END;
                COMMIT;
                """
            )
        with self._transaction() as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key = 'schemaVersion'"
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO meta(key, value) VALUES ('schemaVersion', ?)",
                    (str(USER_SCHEMA_VERSION),),
                )
            elif row["value"] != str(USER_SCHEMA_VERSION):
                raise ValueError("unsupported user store schema")
    def _switch_to_wal(self) -> None:
        """Enable WAL, retrying the lock upgrade SQLite's busy handler skips.

        The CLI and every Lark worker may open this file at once; see
        ``LarkStateStore._switch_to_wal`` for why the first switch can fail
        immediately for the loser and why retrying converges.
        """

        give_up_at = time.monotonic() + _SQLITE_BUSY_MS / 1000
        while True:
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError:
                if time.monotonic() >= give_up_at:
                    raise
                time.sleep(0.05)
    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")
    def get_user(self, user_key: str) -> User | None:
        with self._lock:
            return self._user(self._db, user_key)
    def list_users(self) -> tuple[User, ...]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM users ORDER BY user_key").fetchall()
        return tuple(_user(row) for row in rows)
    def events(self, user_key: str, *, limit: int | None = None) -> tuple[UserEvent, ...]:
        """Events for one user, oldest first; ``limit`` keeps the newest N."""

        with self._lock:
            if limit is None:
                rows = self._db.execute(
                    "SELECT * FROM user_events WHERE user_key = ? ORDER BY id",
                    (user_key,),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM (SELECT * FROM user_events WHERE user_key = ?"
                    " ORDER BY id DESC LIMIT ?) ORDER BY id",
                    (user_key, limit),
                ).fetchall()
        return tuple(_event(row) for row in rows)
    def add_user(
        self,
        *,
        kind: str,
        confirmed_by: str,
        owner: str | None = None,
        nickname: str | None = None,
        real_name: str | None = None,
        display_name: str | None = None,
        now_ms: int | None = None,
    ) -> User:
        """Add one person.  A member's key is its owner's slug; a guest's is
        random, because names repeat and change and a key must do neither."""

        actor = _required(confirmed_by, "confirmed_by")
        if kind not in USER_KINDS:
            raise UserStoreError(
                ipc_errors.INVALID_ARGUMENT,
                f"kind must be one of {', '.join(USER_KINDS)}",
            )
        owner = _text(owner)
        if kind == "member" and owner is None:
            raise UserStoreError(USER_OWNER_REQUIRED, "a member requires an owner")
        if owner is not None and is_identity_id_segment(owner.lower()):
            raise UserStoreError(
                ipc_errors.INVALID_ARGUMENT,
                "owner must not have an exact identity id shape",
            )
        if kind == "guest" and owner is not None:
            raise UserStoreError(
                USER_OWNER_FORBIDDEN,
                "a guest has no owner: a guest is never anyone's authorisation",
            )
        now = _now_ms() if now_ms is None else now_ms
        names = {
            "nickname": _text(nickname),
            "real_name": _text(real_name),
            "display_name": _text(display_name),
        }
        with self._transaction() as db:
            if kind == "member":
                assert owner is not None
                user_key = _owner_key(owner)
                if self._user(db, user_key) is not None:
                    raise UserStoreError(
                        USER_EXISTS, f"user {user_key!r} already exists"
                    )
            else:
                user_key = _GUEST_PREFIX + uuid4().hex[:8]
                while self._user(db, user_key) is not None:
                    user_key = _GUEST_PREFIX + uuid4().hex[:8]
            db.execute(
                "INSERT INTO users(user_key, kind, owner, nickname, real_name,"
                " display_name, entity_token, created_at_ms, updated_at_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_key,
                    kind,
                    owner,
                    names["nickname"],
                    names["real_name"],
                    names["display_name"],
                    uuid4().hex,
                    now,
                    now,
                ),
            )
            self._record(
                db,
                user_key,
                "add",
                actor,
                now,
                {"kind": kind, "owner": owner, **_camel(names)},
            )
            user = self._mirror(db, user_key)
        return user
    def update_user(
        self,
        user_key: str,
        *,
        confirmed_by: str,
        nickname: str | None = None,
        real_name: str | None = None,
        display_name: str | None = None,
        now_ms: int | None = None,
    ) -> User:
        """Change names only.  ``None`` leaves a name as it is; kind and owner
        never change here, because changing them changes whose authorisation
        this person's messages carry."""

        actor = _required(confirmed_by, "confirmed_by")
        changes = {
            column: text
            for column, text in (
                ("nickname", _text(nickname)),
                ("real_name", _text(real_name)),
                ("display_name", _text(display_name)),
            )
            if text is not None
        }
        if not changes:
            raise UserStoreError(
                ipc_errors.INVALID_ARGUMENT, "update_user needs at least one name"
            )
        now = _now_ms() if now_ms is None else now_ms
        with self._transaction() as db:
            before = self._existing(db, user_key)
            assignments = ", ".join(f"{column} = ?" for column in changes)
            db.execute(
                f"UPDATE users SET {assignments}, updated_at_ms = ?"  # noqa: S608 - fixed column names
                " WHERE user_key = ?",
                (*changes.values(), now, user_key),
            )
            self._record(
                db,
                user_key,
                "update",
                actor,
                now,
                {
                    "before": _camel(
                        {column: getattr(before, column) for column in changes}
                    ),
                    "after": _camel(changes),
                },
            )
            user = self._mirror(db, user_key)
        return user
    @staticmethod
    def _record(
        db: sqlite3.Connection,
        user_key: str,
        event: str,
        actor: str,
        at_ms: int,
        detail: dict[str, Any],
    ) -> None:
        db.execute(
            "INSERT INTO user_events(user_key, event, actor, at_ms, detail)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_key, event, actor, at_ms, json.dumps(detail, sort_keys=True)),
        )
    def _mirror(self, db: sqlite3.Connection, user_key: str) -> User:
        """Refresh ``H/users/<key>/profile.json`` inside the write transaction.

        Written before COMMIT on purpose: an unsafe home (a symlink, a wrong
        owner or mode) raises, the transaction rolls back, and the refusal
        leaves no row behind.  A crash after the mirror but before COMMIT
        leaves a mirror ahead of the database, which is harmless because
        the mirror is never read as authority.
        """

        user = self._existing(db, user_key)
        if self._hyprial_home is not None:
            ensure_user_home(self._hyprial_home, user)
        return user
    @staticmethod
    def _user(db: sqlite3.Connection, user_key: str) -> User | None:
        row = db.execute(
            "SELECT * FROM users WHERE user_key = ?", (user_key,)
        ).fetchone()
        return None if row is None else _user(row)
    def _existing(self, db: sqlite3.Connection, user_key: str) -> User:
        user = self._user(db, user_key)
        if user is None:
            raise UserStoreError(USER_NOT_FOUND, f"no user {user_key!r}")
        return user
