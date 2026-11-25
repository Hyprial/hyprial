"""Pure account-invite records: decode, identify, never a token (§3 O2c).

One account-bound invite is one row of the Casdoor ``hyprialInvites``
property.  The row holds the invite *payload* — the part of a
``v2.<payload>`` invite link that names the org, the inviter and the
space — plus the time the account accepted it on the landing page.

Two rules shape this module:

* **The payload is a bearer secret.**  It redeems a Tailnet membership, so
  it never enters a view: :class:`InviteView` carries an identifier and
  the display fields only.
* **Decoding is lenient.**  A row the daemon cannot decode still has to be
  shown and still has to fail on its own, so an unreadable payload becomes
  an ``"unknown"`` view with a payload-derived identifier instead of
  failing the whole pickup.

The identifier is what a confirmation is bound to: it is derived from the
invite's own fields, so the daemon can recompute it from a decoded link
and refuse a mismatched confirmation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "INVITE_ID_PREFIX",
    "UNKNOWN",
    "InviteView",
    "invite_identifier",
    "invite_link",
    "invite_view",
    "invite_views",
]

UNKNOWN = "unknown"
"""What an undecodable invite names for org/inviter/space (§5.3 display)."""

INVITE_ID_PREFIX = "inv-"
"""The identifier is namespaced so it can never be mistaken for a link."""

_ID_LENGTH = 20


def invite_identifier(text: str) -> str:
    """Digest the complete invite, including bearer and expiry, not its label.

    Transport wrapping and JSON whitespace do not change the identity.
    This is a confirmation binding, not proof of authorization.
    """
    from hyprial.identity.impl.org_directory.invite import _extract_payload

    candidate = text.strip().strip("\"'")
    if candidate.startswith(("hyprial-invite:", "v2.", "https://", "hyprial://")):
        candidate = _extract_payload(text)
    record = _decode_payload(candidate)
    material = (
        json.dumps(dict(record), sort_keys=True, separators=(",", ":"))
        if record is not None else candidate
    )
    return _payload_identifier(material)


def _payload_identifier(payload: str) -> str:
    """Identifier for a payload whose fields could not be read."""

    return INVITE_ID_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()[
        :_ID_LENGTH
    ]


@dataclass(frozen=True, slots=True)
class InviteView:
    """One invite as the CLI and the daemon may show it.  No payload."""

    invite_id: str
    org: str
    inviter: str
    space_id: str
    accepted_at: str | None = None

    @property
    def unknown(self) -> bool:
        """True when this row could not be decoded; it can only fail alone."""

        return self.org == UNKNOWN and self.space_id == UNKNOWN

    def row(self) -> dict[str, Any]:
        """The wire/JSON shape (``org.invites.list`` pending rows)."""

        return {
            "inviteId": self.invite_id,
            "org": self.org,
            "inviter": self.inviter,
            "spaceId": self.space_id,
            "acceptedAt": self.accepted_at,
        }


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value and value != UNKNOWN else None


def _decode_payload(payload: str) -> Mapping[str, Any] | None:
    """The payload's JSON object, or None when it cannot be read."""

    try:
        padded = payload + "=" * (-len(payload) % 4)
        record = json.loads(
            base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
        )
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return record if isinstance(record, Mapping) else None


def invite_view(payload: object, accepted_at: object = None) -> InviteView:
    """One account row as a view; an unreadable payload degrades to unknown."""

    stamp = accepted_at if isinstance(accepted_at, str) and accepted_at else None
    if not isinstance(payload, str) or not payload:
        return InviteView(
            invite_id=_payload_identifier(""),
            org=UNKNOWN,
            inviter=UNKNOWN,
            space_id=UNKNOWN,
            accepted_at=stamp,
        )
    record = _decode_payload(payload) or {}
    org = _text(record.get("org"))
    inviter = _text(record.get("inviter"))
    space_id = _text(record.get("spaceId"))
    if org is None or inviter is None or space_id is None:
        return InviteView(
            invite_id=_payload_identifier(payload),
            org=UNKNOWN,
            inviter=UNKNOWN,
            space_id=UNKNOWN,
            accepted_at=stamp,
        )
    return InviteView(
        invite_id=invite_identifier(payload),
        org=org,
        inviter=inviter,
        space_id=space_id,
        accepted_at=stamp,
    )


def invite_views(rows: Iterable[object]) -> tuple[InviteView, ...]:
    """Views for a sequence of account rows (``{payload, acceptedAt}``).

    A row that is not an object, or that carries no payload string, is a
    malformed account property, not a single bad invite: the caller turns
    the ``ValueError`` into ``INVALID_ARGUMENT`` rather than silently
    joining a subset.
    """

    views: list[InviteView] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("an account invite row must be an object")
        payload = row.get("payload")
        if not isinstance(payload, str) or not payload:
            raise ValueError("an account invite row needs a payload string")
        accepted_at = row.get("acceptedAt")
        if accepted_at is not None and not isinstance(accepted_at, str):
            raise ValueError("an account invite acceptedAt must be a string or null")
        views.append(invite_view(payload, accepted_at))
    return tuple(views)


def invite_link(payload: str) -> str:
    """The bare ``v2.<payload>`` form the daemon's join accepts (§5.1)."""

    return f"v2.{payload}"
