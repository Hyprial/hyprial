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
    Ambiguous,
    CHANNEL_TYPES,
    ResolvedUser,
    USER_ACCOUNT_CONFLICT,
    USER_ACCOUNT_NOT_FOUND,
    USER_CHANNEL_CONFLICT,
    USER_EXISTS,
    USER_KINDS,
    USER_NOT_FOUND,
    USER_OWNER_FORBIDDEN,
    USER_OWNER_REQUIRED,
    USER_SCHEMA_VERSION,
    User,
    UserAccount,
    UserChannel,
    UserEvent,
    UserStoreError,
    _GUEST_PREFIX,
    _SQLITE_BUSY_MS,
    _account,
    _camel,
    _channel,
    _event,
    _now_ms,
    _owner_key,
    _required,
    _text,
    _user,
)

class UserStore():
    """SQLite at ``state_dir/users.sqlite3``; the authority for users.

    ``hyprial_home`` is optional because the store has two kinds of caller:
    the CLI writes and so keeps each user's home mirror current, while the
    Lark worker only resolves and must not create directories under H.
    """
    def __init__(self, path: Path, *, hyprial_home: Path | None = None) -> None:
        self.path = path
        self._hyprial_home = hyprial_home
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Explicit transaction control, as in LarkStateStore: the reads of a
        # read-check-write (e.g. "is this open_id bound to someone else?")
        # must sit inside the same write transaction as the write.
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

                CREATE TABLE IF NOT EXISTS user_accounts (
                    user_key TEXT NOT NULL
                        REFERENCES users(user_key) ON DELETE RESTRICT,
                    platform TEXT NOT NULL DEFAULT 'lark',
                    adapter TEXT NOT NULL,
                    open_id TEXT NOT NULL,
                    union_id TEXT,
                    confirmed_by TEXT NOT NULL CHECK (confirmed_by <> ''),
                    confirmed_at_ms INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    UNIQUE (adapter, open_id)
                );
                CREATE INDEX IF NOT EXISTS user_accounts_by_union
                    ON user_accounts(union_id);

                CREATE TABLE IF NOT EXISTS user_channels (
                    user_key TEXT NOT NULL
                        REFERENCES users(user_key) ON DELETE RESTRICT,
                    adapter TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    chat_type TEXT NOT NULL CHECK (chat_type IN ('p2p', 'group')),
                    route_name TEXT,
                    UNIQUE (adapter, chat_id)
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
    def accounts(self, user_key: str) -> tuple[UserAccount, ...]:
        with self._lock:
            return self._accounts(self._db, user_key)
    def channels(self, user_key: str) -> tuple[UserChannel, ...]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM user_channels WHERE user_key = ?"
                " ORDER BY adapter, chat_id",
                (user_key,),
            ).fetchall()
        return tuple(_channel(row) for row in rows)
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
    def resolve_account(
        self, adapter: str, open_id: str, union_id: str | None = None
    ) -> ResolvedUser | Ambiguous | None:
        """Who a platform account belongs to, from confirmed bindings only.

        1. This adapter's exact ``open_id`` binding wins.
        2. Else any binding sharing ``union_id`` (open_ids are per App, so a
           person confirmed under one App is otherwise invisible to another).
        3. A union_id bound to two different people is :class:`Ambiguous`.

        ``None`` means no confirmation names this account; the caller keeps
        its own fallback.  Nothing here matches on a name.
        """

        with self._lock:
            exact = self._db.execute(
                "SELECT * FROM user_accounts WHERE adapter = ? AND open_id = ?",
                (adapter, open_id),
            ).fetchone()
            if exact is not None:
                account = _account(exact)
                user = self._user(self._db, account.user_key)
                assert user is not None  # foreign key
                return ResolvedUser(user, account, "open_id")
            union = _text(union_id)
            if union is None:
                return None
            rows = self._db.execute(
                "SELECT * FROM user_accounts WHERE union_id = ?"
                " ORDER BY adapter, open_id",
                (union,),
            ).fetchall()
            accounts = [_account(row) for row in rows]
            keys = sorted({account.user_key for account in accounts})
            if not keys:
                return None
            users = tuple(self._user(self._db, key) for key in keys)
        if len(keys) > 1:
            return Ambiguous(tuple(user for user in users if user is not None))
        (user,) = users
        assert user is not None  # foreign key
        return ResolvedUser(user, accounts[0], "union_id")
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
    def bind_account(
        self,
        user_key: str,
        *,
        adapter: str,
        open_id: str,
        confirmed_by: str,
        union_id: str | None = None,
        source: str | None = None,
        platform: str = "lark",
        dm_chat_id: str | None = None,
        now_ms: int | None = None,
    ) -> tuple[UserAccount, bool]:
        """Confirm that ``(adapter, open_id)`` is this person.

        Returns the account and whether anything changed.  Binding an account
        already confirmed for someone else is refused, never moved: a silent
        move would re-attribute every later message.  Re-binding to the same
        person is a no-op (no event), except that a union_id missing from
        the first confirmation is filled in.  ``dm_chat_id`` records the p2p
        channel in the same transaction, so a refused channel leaves no
        half-made binding.
        """

        actor = _required(confirmed_by, "confirmed_by")
        adapter = _required(adapter, "adapter")
        open_id = _required(open_id, "open_id")
        union = _text(union_id)
        origin = _text(source) or f"manual:{actor}"
        now = _now_ms() if now_ms is None else now_ms
        with self._transaction() as db:
            self._existing(db, user_key)
            row = db.execute(
                "SELECT * FROM user_accounts WHERE adapter = ? AND open_id = ?",
                (adapter, open_id),
            ).fetchone()
            changed = False
            if row is None:
                db.execute(
                    "INSERT INTO user_accounts(user_key, platform, adapter,"
                    " open_id, union_id, confirmed_by, confirmed_at_ms, source)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_key, platform, adapter, open_id, union, actor, now, origin),
                )
                self._record(
                    db,
                    user_key,
                    "bind",
                    actor,
                    now,
                    {
                        "adapter": adapter,
                        "openId": open_id,
                        "unionId": union,
                        "source": origin,
                    },
                )
                changed = True
            else:
                existing = _account(row)
                if existing.user_key != user_key:
                    raise UserStoreError(
                        USER_ACCOUNT_CONFLICT,
                        f"{adapter} {open_id} is already bound to user"
                        f" {existing.user_key!r}; unbind it there first",
                    )
                if union is not None and existing.union_id not in (None, union):
                    raise UserStoreError(
                        USER_ACCOUNT_CONFLICT,
                        f"{adapter} {open_id} is bound with union_id"
                        f" {existing.union_id!r}, not {union!r}; unbind it first",
                    )
                if union is not None and existing.union_id is None:
                    db.execute(
                        "UPDATE user_accounts SET union_id = ?"
                        " WHERE adapter = ? AND open_id = ?",
                        (union, adapter, open_id),
                    )
                    self._record(
                        db,
                        user_key,
                        "bind",
                        actor,
                        now,
                        {"adapter": adapter, "openId": open_id, "unionIdAdded": union},
                    )
                    changed = True
            if dm_chat_id is not None:
                changed = (
                    self._add_channel(
                        db,
                        user_key,
                        adapter=adapter,
                        chat_id=_required(dm_chat_id, "dm_chat_id"),
                        chat_type="p2p",
                        route_name=None,
                        actor=actor,
                        now=now,
                    )
                    or changed
                )
            account = _account(
                db.execute(
                    "SELECT * FROM user_accounts WHERE adapter = ? AND open_id = ?",
                    (adapter, open_id),
                ).fetchone()
            )
            if changed:
                self._mirror(db, user_key)
        return account, changed
    def unbind_account(
        self,
        user_key: str,
        *,
        adapter: str,
        open_id: str,
        confirmed_by: str,
        now_ms: int | None = None,
    ) -> UserAccount:
        actor = _required(confirmed_by, "confirmed_by")
        now = _now_ms() if now_ms is None else now_ms
        with self._transaction() as db:
            self._existing(db, user_key)
            row = db.execute(
                "SELECT * FROM user_accounts WHERE adapter = ? AND open_id = ?",
                (adapter, open_id),
            ).fetchone()
            if row is None:
                raise UserStoreError(
                    USER_ACCOUNT_NOT_FOUND, f"{adapter} {open_id} is not bound"
                )
            account = _account(row)
            if account.user_key != user_key:
                raise UserStoreError(
                    USER_ACCOUNT_CONFLICT,
                    f"{adapter} {open_id} is bound to user {account.user_key!r},"
                    f" not {user_key!r}",
                )
            db.execute(
                "DELETE FROM user_accounts WHERE adapter = ? AND open_id = ?",
                (adapter, open_id),
            )
            self._record(
                db,
                user_key,
                "unbind",
                actor,
                now,
                {
                    "adapter": adapter,
                    "openId": open_id,
                    "unionId": account.union_id,
                    # Who confirmed the binding now being removed: the
                    # deleted row is gone, so the event is its only trace.
                    "confirmedBy": account.confirmed_by,
                    "source": account.source,
                },
            )
            self._mirror(db, user_key)
        return account
    def add_channel(
        self,
        user_key: str,
        *,
        adapter: str,
        chat_id: str,
        confirmed_by: str,
        chat_type: str = "p2p",
        route_name: str | None = None,
        now_ms: int | None = None,
    ) -> bool:
        """Record a chat that reaches this person; ``False`` if already so."""

        actor = _required(confirmed_by, "confirmed_by")
        now = _now_ms() if now_ms is None else now_ms
        with self._transaction() as db:
            self._existing(db, user_key)
            changed = self._add_channel(
                db,
                user_key,
                adapter=_required(adapter, "adapter"),
                chat_id=_required(chat_id, "chat_id"),
                chat_type=chat_type,
                route_name=_text(route_name),
                actor=actor,
                now=now,
            )
            if changed:
                self._mirror(db, user_key)
        return changed
    def _add_channel(
        self,
        db: sqlite3.Connection,
        user_key: str,
        *,
        adapter: str,
        chat_id: str,
        chat_type: str,
        route_name: str | None,
        actor: str,
        now: int,
    ) -> bool:
        if chat_type not in CHANNEL_TYPES:
            raise UserStoreError(
                ipc_errors.INVALID_ARGUMENT,
                f"chat_type must be one of {', '.join(CHANNEL_TYPES)}",
            )
        row = db.execute(
            "SELECT * FROM user_channels WHERE adapter = ? AND chat_id = ?",
            (adapter, chat_id),
        ).fetchone()
        if row is not None:
            existing = _channel(row)
            if existing.user_key != user_key:
                raise UserStoreError(
                    USER_CHANNEL_CONFLICT,
                    f"{adapter} chat {chat_id} already reaches user"
                    f" {existing.user_key!r}",
                )
            return False
        db.execute(
            "INSERT INTO user_channels(user_key, adapter, chat_id, chat_type,"
            " route_name) VALUES (?, ?, ?, ?, ?)",
            (user_key, adapter, chat_id, chat_type, route_name),
        )
        self._record(
            db,
            user_key,
            "channel",
            actor,
            now,
            {
                "adapter": adapter,
                "chatId": chat_id,
                "chatType": chat_type,
                "routeName": route_name,
            },
        )
        return True
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
            ensure_user_home(
                self._hyprial_home, user, self._accounts(db, user_key)
            )
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
    @staticmethod
    def _accounts(db: sqlite3.Connection, user_key: str) -> tuple[UserAccount, ...]:
        rows = db.execute(
            "SELECT * FROM user_accounts WHERE user_key = ?"
            " ORDER BY adapter, open_id",
            (user_key,),
        ).fetchall()
        return tuple(_account(row) for row in rows)
