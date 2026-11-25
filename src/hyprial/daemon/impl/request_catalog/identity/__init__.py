"""Identity requests over the name-only person registry (``state/users.sqlite3``).

R3 of the user-to-IM binding removal (design #2950 8.5): the registry keeps
people by name only.  An IM account says nothing about who a person is here,
so there is no resolver between these requests and the user store: listing
reads the store directly and never creates it; adding opens (and so creates)
it and mirrors the user's home.  ``identity.users.list`` keeps the legacy IPC
parameter domain and error order; the HTTP listener applies the narrower
declared schema before dispatch.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .read_specs import USERS_LIST_SPEC
from .writes import IDENTITY_WRITE_BINDINGS, opened_users, unavailable


PARTIAL_MIGRATED_METHODS = frozenset({'identity.users.list', 'identity.users.add'})


def users_list(app: Any, params: Mapping[str, Any], *, trusted_message_origin: str | None = None):
    kind, user_key = params.get('kind'), params.get('user')
    with opened_users(app, create=False) as users:
        try:
            records = () if users is None else users.list_users()
        except Exception as error:  # noqa: BLE001 - source boundary
            raise unavailable(error) from error
    return {'users': [
        user.to_json() for user in records
        if (kind is None or user.kind == kind) and (user_key is None or user.user_key == user_key)
    ]}


IDENTITY_READ_BINDINGS = ((USERS_LIST_SPEC, users_list),)

IDENTITY_BINDINGS = (*IDENTITY_READ_BINDINGS, *IDENTITY_WRITE_BINDINGS)

IDENTITY_PRODUCTION_READ_BINDINGS = tuple(
    (spec, handler, None, True) for spec, handler in IDENTITY_READ_BINDINGS
)
