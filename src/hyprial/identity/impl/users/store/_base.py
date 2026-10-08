from __future__ import annotations

from typing import Any
from dataclasses import dataclass
from hyprial.kernel import ipc_errors
import json
import sqlite3
import time

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
USER_SCHEMA_VERSION = 1
USER_KINDS = ("member", "guest")
CHANNEL_TYPES = ("p2p", "group")
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
def _owner_key(owner: str) -> str:
    # The same slug squire uses for its owner_key, so one person has one key
    # on this machine.  Imported here, not at module top: squire.setup pulls
    # in management and the daemon's desired state, and this module is
    # imported by the Lark adapter.
    from hyprial.identity import identity_slug

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
