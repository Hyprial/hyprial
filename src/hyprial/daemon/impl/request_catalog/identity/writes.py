"""``identity.users.add`` and the user-store access both identity requests share.

Only the local operator adds a person: a worker origin is refused with
``IDENTITY_OVERRIDE_FORBIDDEN`` before any field is read.  The key is
store-generated (member: slug of the owner; guest: random), so ``user`` and any
other field outside ``UserStore.add_user``'s inputs are refused, never ignored.
A store that cannot be opened or read is ``IDENTITY_SOURCE_UNAVAILABLE``, never
an empty answer.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import sqlite3
from typing import Any

from hyprial.identity import UserStore, UserStoreError
from hyprial.kernel import DaemonRequestError, ipc_errors

from .write_specs import USERS_ADD_SPEC


_USERS_ADD_FIELDS = frozenset(
    {"kind", "confirmedBy", "owner", "displayName", "nickname", "realName"}
)


@contextmanager
def opened_users(app: Any, *, create: bool) -> Iterator[UserStore | None]:
    path = app.state_dir / "users.sqlite3"
    if not create and not path.is_file():
        yield None
        return
    try:
        users = UserStore(path, hyprial_home=app.hyprial_home)
    except Exception as error:  # noqa: BLE001 - source boundary
        raise unavailable(error) from error
    try:
        yield users
    finally:
        users.close()


def unavailable(error: BaseException, source: str = "local users source") -> DaemonRequestError:
    return DaemonRequestError(
        ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
        f"{source} is unavailable: {type(error).__name__}",
    )


def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, f"{name} must be a non-empty string"
        )
    return value.strip()


def users_add(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None):
    if trusted_message_origin == "worker":
        raise DaemonRequestError(
            ipc_errors.IDENTITY_OVERRIDE_FORBIDDEN,
            "identity mutation is available only to the local operator",
        )
    unknown = sorted(set(params) - _USERS_ADD_FIELDS)
    if unknown:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"users.add does not take {', '.join(unknown)}; the key is store-generated",
        )
    if params.get("kind") == "guest" and not params.get("displayName"):
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "a guest requires a displayName")
    try:
        with opened_users(app, create=True) as users:
            assert users is not None
            user = users.add_user(
                kind=_required(params.get("kind"), "kind"),
                owner=params.get("owner"),
                nickname=params.get("nickname"),
                real_name=params.get("realName"),
                display_name=params.get("displayName"),
                confirmed_by=_required(params.get("confirmedBy"), "confirmedBy"),
            )
    except UserStoreError as error:
        raise DaemonRequestError(error.code, str(error)) from error
    except (OSError, sqlite3.Error) as error:
        raise unavailable(error, "identity source") from error
    return {"user": user.to_json()}


IDENTITY_WRITE_BINDINGS = ((USERS_ADD_SPEC, users_add),)
