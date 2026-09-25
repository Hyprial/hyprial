"""The per-machine user store: who a person is, and who confirmed it.

Before this, what hyprial knew about a person was split across the Lark
``identities`` table (open_id, one display name, a bare ``hyprial_owner``
string), squire's ``users.json`` (owners only, one App's open_id) and a
hand-kept ``org-context.md``; no key joined them, and a guest had nowhere to
live at all.  This store gives every person one row, whether or not they
have a hyprial login (Allen, 2026-09-25: 「访客也进同一张表标记为guest」).

Authority rules, all enforced here rather than left to callers:

- A member names an owner and its key is that owner's slug; a guest never
  has an owner, so nothing a guest says can be read as anyone's
  authorisation.  Both rules are checked in code (for a named error) and by
  a table CHECK (so a future caller that skips the code path still cannot
  write the forbidden shape).
- Only a confirmed binding makes a platform account resolve to a person, and
  every confirmation records who made it (``confirmed_by``).  A coordinator
  agent may confirm (「允许协调者绑定」), which is exactly why the name of
  the confirmer must be kept: it is the only way to audit a wrong binding.
- An account already bound to one person is never silently moved to
  another; the caller must unbind first, which leaves an event.
- ``user_events`` is append-only (triggers refuse UPDATE and DELETE), and
  every write adds one row whose ``actor`` is the confirmer.

The store is node-local by ruling (「用户库每机一份」): nothing here is
replicated over the mesh.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from hyprial.contracts import ipc_errors

from .home import ensure_user_home

__all__ = [
    "Ambiguous",
    "ResolvedUser",
    "USER_KINDS",
    "User",
    "UserAccount",
    "UserChannel",
    "UserEvent",
    "UserStore",
    "UserStoreError",
]

USER_SCHEMA_VERSION = 1
USER_KINDS = ("member", "guest")
CHANNEL_TYPES = ("p2p", "group")

# CLI-facing refusal codes.  They are rendered to a person or emitted in
# ``--json`` and no other process branches on them, so by the membership
# line in ``contracts/ipc_errors.py`` they stay out of that registry.
USER_NOT_FOUND = "USER_NOT_FOUND"
USER_EXISTS = "USER_EXISTS"
USER_OWNER_REQUIRED = "USER_OWNER_REQUIRED"
USER_OWNER_FORBIDDEN = "USER_OWNER_FORBIDDEN"
USER_ACCOUNT_CONFLICT = "USER_ACCOUNT_CONFLICT"
USER_ACCOUNT_NOT_FOUND = "USER_ACCOUNT_NOT_FOUND"
USER_CHANNEL_CONFLICT = "USER_CHANNEL_CONFLICT"

_GUEST_PREFIX = "guest-"
_SQLITE_BUSY_MS = 5_000


class UserStoreError(ValueError):
    """A refused user-store write, carrying a stable code for the CLI."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class User:
    user_key: str
    kind: str
    owner: str | None
    nickname: str | None
    real_name: str | None
    display_name: str | None
    entity_token: str
    created_at_ms: int
    updated_at_ms: int

    @property
    def shown_name(self) -> str | None:
        """The one name a reader sees: display, else nickname, else real name."""

        return self.display_name or self.nickname or self.real_name

    def to_json(self) -> dict[str, Any]:
        return {
            "userKey": self.user_key,
            "kind": self.kind,
            "owner": self.owner,
            "nickname": self.nickname,
            "realName": self.real_name,
            "displayName": self.display_name,
            "createdAtMs": self.created_at_ms,
            "updatedAtMs": self.updated_at_ms,
        }


@dataclass(frozen=True, slots=True)
class UserAccount:
    user_key: str
    platform: str
    adapter: str
    open_id: str
    union_id: str | None
    confirmed_by: str
    confirmed_at_ms: int
    source: str

    def to_json(self) -> dict[str, Any]:
        return {
            "userKey": self.user_key,
            "platform": self.platform,
            "adapter": self.adapter,
            "openId": self.open_id,
            "unionId": self.union_id,
            "confirmedBy": self.confirmed_by,
            "confirmedAtMs": self.confirmed_at_ms,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class UserChannel:
    user_key: str
    adapter: str
    chat_id: str
    chat_type: str
    route_name: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "userKey": self.user_key,
            "adapter": self.adapter,
            "chatId": self.chat_id,
            "chatType": self.chat_type,
            "routeName": self.route_name,
        }


@dataclass(frozen=True, slots=True)
class UserEvent:
    id: int
    user_key: str
    event: str
    actor: str
    at_ms: int
    detail: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "userKey": self.user_key,
            "event": self.event,
            "actor": self.actor,
            "atMs": self.at_ms,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ResolvedUser:
    """A platform account that a person confirmed belongs to ``user``.

    ``matched_by`` says which rule found it: the exact ``open_id`` under this
    adapter, or a ``union_id`` shared with an account confirmed under another
    adapter (open_ids are per App; union_id is stable across Apps).
    """

    user: User
    account: UserAccount
    matched_by: str


@dataclass(frozen=True, slots=True)
class Ambiguous:
    """A union_id bound to accounts of more than one person.

    Returned instead of a first-wins pick: two confirmations that disagree
    are a question for a person, never an answer.
    """

    candidates: tuple[User, ...]

    @property
    def user_keys(self) -> tuple[str, ...]:
        return tuple(user.user_key for user in self.candidates)


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _required(value: str | None, label: str) -> str:
    text = _text(value)
    if text is None:
        raise UserStoreError(ipc_errors.INVALID_ARGUMENT, f"{label} must not be empty")
    return text


def _now_ms() -> int:
    return int(time.time() * 1000)


class UserStore:
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

    # -- schema --------------------------------------------------------------

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

    # -- reads ---------------------------------------------------------------

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

    # -- writes --------------------------------------------------------------

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

    # -- internals -----------------------------------------------------------

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


class LazyUserStore:
    """The read side a long-running process holds: opened on first use.

    A Lark worker lives for days, while ``hyprial user add`` creates
    ``users.sqlite3`` whenever an operator (or the coordinator) first runs it.
    Opening only at worker start would make the first binding on a machine
    invisible until an adapter restart; opening eagerly would create the file
    on machines that never use it.  So: look for the file on each lookup
    until it exists, open it once, then keep it.

    A store that exists but cannot be opened must not break inbound
    delivery -- naming the sender is bookkeeping beside it, exactly like the
    identities lookup it precedes.  The failure is reported once through
    ``on_open_failure`` and the lookup answers ``None``, so the caller falls
    back to #818's identities-only resolution.
    """

    def __init__(
        self,
        path: Path,
        *,
        on_open_failure: Callable[[Exception], None] | None = None,
    ) -> None:
        self._path = path
        self._on_open_failure = on_open_failure
        self._store: UserStore | None = None
        self._failure_reported = False
        self._lock = threading.Lock()

    def _opened(self) -> UserStore | None:
        with self._lock:
            if self._store is not None:
                return self._store
            if not self._path.is_file():
                return None
            try:
                self._store = UserStore(self._path)
            except (NameError, ImportError):
                raise
            except Exception as error:  # noqa: BLE001 - never break inbound
                if not self._failure_reported and self._on_open_failure is not None:
                    self._failure_reported = True
                    self._on_open_failure(error)
                return None
            return self._store

    def resolve_account(
        self, adapter: str, open_id: str, union_id: str | None = None
    ) -> ResolvedUser | Ambiguous | None:
        store = self._opened()
        if store is None:
            return None
        return store.resolve_account(adapter, open_id, union_id)

    def close(self) -> None:
        with self._lock:
            if self._store is not None:
                self._store.close()
                self._store = None


def _owner_key(owner: str) -> str:
    # The same slug squire uses for its owner_key, so one person has one key
    # on this machine.  Imported here, not at module top: squire.setup pulls
    # in management and the daemon's desired state, and this module is
    # imported by the Lark adapter.
    from hyprial.squire.setup import identity_slug

    try:
        key = identity_slug(owner)
    except ValueError as error:
        raise UserStoreError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    if not key[0].isalnum():
        # The key is also a directory name under H/users; "..", "." and
        # friends slug to themselves.
        raise UserStoreError(
            ipc_errors.INVALID_ARGUMENT, f"cannot derive a user key from {owner!r}"
        )
    return key


def _camel(names: dict[str, Any]) -> dict[str, Any]:
    return {
        "".join(
            part if index == 0 else part.capitalize()
            for index, part in enumerate(key.split("_"))
        ): value
        for key, value in names.items()
    }


def _user(row: sqlite3.Row) -> User:
    return User(
        user_key=row["user_key"],
        kind=row["kind"],
        owner=row["owner"],
        nickname=row["nickname"],
        real_name=row["real_name"],
        display_name=row["display_name"],
        entity_token=row["entity_token"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


def _account(row: sqlite3.Row) -> UserAccount:
    return UserAccount(
        user_key=row["user_key"],
        platform=row["platform"],
        adapter=row["adapter"],
        open_id=row["open_id"],
        union_id=row["union_id"],
        confirmed_by=row["confirmed_by"],
        confirmed_at_ms=row["confirmed_at_ms"],
        source=row["source"],
    )


def _channel(row: sqlite3.Row) -> UserChannel:
    return UserChannel(
        user_key=row["user_key"],
        adapter=row["adapter"],
        chat_id=row["chat_id"],
        chat_type=row["chat_type"],
        route_name=row["route_name"],
    )


def _event(row: sqlite3.Row) -> UserEvent:
    return UserEvent(
        id=row["id"],
        user_key=row["user_key"],
        event=row["event"],
        actor=row["actor"],
        at_ms=row["at_ms"],
        detail=json.loads(row["detail"]),
    )
