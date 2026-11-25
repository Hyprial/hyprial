"""The account snapshot the login path hands to the daemon (§3 O2c).

The signed-in account's ``hyprialInvites`` property is read with the user's
own access token, and that token lives in ``secrets/login.json`` — a file
the daemon must never read (the T11 anchor in ``shell/impl/login/flow.py``,
enforced by ``tests/shell/login/device/test_boundaries.py``).  The daemon's
Casdoor service user cannot read user properties either: it only holds the
patched group-member endpoints (``org/network/casdoor/client.py``).

So the *snapshot* crosses the boundary, never the credential: the login
path reads the account once and hands the rows to ``org.invites.list``.
This module owns the shape of that hand-over and nothing else —

* a row is an object with a non-empty ``payload`` string and an optional
  ``acceptedAt`` string;
* a malformed row list is a caller bug, not one bad invite: it is refused
  whole (``ValueError`` → ``INVALID_ARGUMENT``) instead of silently
  dropping rows;
* the payload never leaves the parse: only
  :class:`~hyprial.identity.InviteView` values do.
"""

from __future__ import annotations

from typing import Any

from hyprial.identity import InviteView, invite_views

__all__ = ["snapshot_views"]


def snapshot_views(rows: Any) -> tuple[InviteView, ...]:
    """Validate one account snapshot and return its views.

    ``rows`` is the ``invites`` parameter of ``org.invites.list``: the
    ``hyprialInvites`` array exactly as the account client read it.
    """

    if not isinstance(rows, (list, tuple)):
        raise ValueError("invites must be an array of account invite rows")
    return invite_views(rows)
