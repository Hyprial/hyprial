"""The per-machine user store: a name-only registry of people.

One row per person, member or guest, whether or not they have a hyprial login
(Allen, 2026-09-25: 「访客也进同一张表标记为guest」).  A member names an
owner and its key is that owner's slug; a guest never has an owner, so nothing
a guest says can be read as anyone's authorisation.  Both rules are checked in
code (for a named error) and by a table CHECK.  Every write records who
confirmed it in the append-only ``user_events`` table.

R3 of the user-to-IM binding removal (design #2950 8.5, owner ruling
2026-11-19): an IM account is optional information about a person and an IM
sender is simply the person who owns that account, so the store keeps no
platform accounts or chats.  The store is node-local by ruling
(「用户库每机一份」): nothing here is replicated over the mesh.
"""

from __future__ import annotations

from typing import Any
from dataclasses import dataclass
from hyprial.kernel import ipc_errors
import json
import sqlite3
import time

USER_SCHEMA_VERSION = 1
USER_KINDS = ("member", "guest")
USER_NOT_FOUND = "USER_NOT_FOUND"
USER_EXISTS = "USER_EXISTS"
USER_OWNER_REQUIRED = "USER_OWNER_REQUIRED"
USER_OWNER_FORBIDDEN = "USER_OWNER_FORBIDDEN"
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
def _event(row: sqlite3.Row) -> UserEvent:
    return UserEvent(
        id=row["id"],
        user_key=row["user_key"],
        event=row["event"],
        actor=row["actor"],
        at_ms=row["at_ms"],
        detail=json.loads(row["detail"]),
    )
