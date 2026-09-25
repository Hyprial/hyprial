"""User-home provisioning: ``H/users/<user_key>/``, fenced like agent homes.

The user store (``state/users.sqlite3``) is the authority.  This module only
keeps the filesystem half: the private directories a person's preferences
and (later) credentials will live in, and ``profile.json``, a readable mirror
of the store's row.  Nothing reads the mirror to decide anything.

The fences are the agent home's, reused rather than re-derived: every
directory this module manages is lstat-checked for a symlink, the current
uid and mode 0700, so a planted link cannot redirect a write outside H and a
loosened directory is refused instead of silently used.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from hyprial.agents.home import AgentHomeError, AgentHomeProvisioner

if TYPE_CHECKING:
    from .store import User, UserAccount

__all__ = ["PROFILE_NAME", "UserHomeError", "ensure_user_home"]

PROFILE_NAME = "profile.json"
_PROFILE_SCHEMA = 1
_USER_SUBDIRECTORIES = ("config", "secrets", "state")
#: A user key becomes one path segment under ``H/users``.  Owner slugs are
#: ``[a-z0-9._-]`` but may still be ``..``; requiring an alphanumeric first
#: character rules out every traversal spelling.
_SAFE_KEY = re.compile(r"[a-z0-9][a-z0-9._-]*")


class UserHomeError(RuntimeError):
    """A sanitized home error carrying only category, user key, and phase."""

    def __init__(self, category: str, user_key: str, phase: str) -> None:
        self.category = category
        self.user_key = user_key
        self.phase = phase
        super().__init__(
            f"user home {category}: user={user_key!r} phase={phase!r}"
        )


def ensure_user_home(
    hyprial_home: Path,
    user: User,
    accounts: Sequence[UserAccount] = (),
) -> Path:
    """Create or validate ``H/users/<key>/{config,secrets,state}``; refresh
    ``profile.json``.  Returns the user's home directory.

    Idempotent: existing directories are validated, never recreated.  The
    mirror carries no secrets -- account rows hold platform ids and who
    confirmed them, nothing that authenticates.
    """

    home = Path(hyprial_home)
    key = user.user_key
    if not home.is_absolute():
        raise UserHomeError("unsafe-path", key, "home-root")
    if _SAFE_KEY.fullmatch(key) is None:
        raise UserHomeError("unsafe-key", key, "reserve")
    users_root = home / "users"
    root = users_root / key
    # H's own prefix is the operator's filesystem (see AgentHomeProvisioner
    # ._prepare_root); the fence starts at H.
    _ensure_directory(home, key, "home-root", mode=None, parents=True)
    _ensure_directory(users_root, key, "users-root", mode=0o700)
    _ensure_directory(root, key, "user-root", mode=0o700)
    for name in _USER_SUBDIRECTORIES:
        _ensure_directory(root / name, key, f"user-{name}", mode=0o700)
    _replace_private_json(root / PROFILE_NAME, _profile(user, accounts), key)
    return root


def _ensure_directory(
    path: Path, key: str, phase: str, *, mode: int | None, parents: bool = False
) -> None:
    if not (path.exists() or path.is_symlink()):
        try:
            path.mkdir(mode=0o700, parents=parents)
        except FileExistsError:
            pass  # a concurrent writer made it; validated below either way
        except OSError as error:
            raise UserHomeError("io", key, phase) from error
    try:
        AgentHomeProvisioner.safe_directory(path, key, phase, mode=mode)
    except AgentHomeError as error:
        raise UserHomeError(error.category, key, phase) from error


def _profile(user: User, accounts: Sequence[UserAccount]) -> dict[str, object]:
    return {
        "schemaVersion": _PROFILE_SCHEMA,
        # Says so in the file: an operator reading it must not edit it
        # expecting the change to take effect.
        "authority": "state/users.sqlite3",
        "userKey": user.user_key,
        "kind": user.kind,
        "owner": user.owner,
        "nickname": user.nickname,
        "realName": user.real_name,
        "displayName": user.display_name,
        "accounts": [
            {
                "platform": account.platform,
                "adapter": account.adapter,
                "openId": account.open_id,
                "unionId": account.union_id,
                "confirmedBy": account.confirmed_by,
                "confirmedAtMs": account.confirmed_at_ms,
                "source": account.source,
            }
            for account in accounts
        ],
        "updatedAt": datetime.fromtimestamp(user.updated_at_ms / 1000, UTC)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def _replace_private_json(path: Path, value: object, key: str) -> None:
    """Write 0600 from the first byte, then rename over the old mirror.

    The temporary file is created exclusively at 0600 (never written and
    then chmod-ed, which leaves a readable window), and ``os.replace``
    renames over the directory entry without following a link planted at
    ``path``.
    """

    encoded = (json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n").encode()
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    except OSError as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise UserHomeError("io", key, "write-profile") from error
