# Decision notes (service grant exposing side, 2026-11-26):
# - The admission key of a device is the entry under THIS device's server
#   public key in that device's row of the sidecar's own roster, as returned by
#   membership-status. It is never read from the directory device row.
# - A row counts only when its tailnet is ready and the row is active: a
#   device removed from the org is a tombstone, and a tailnet whose local
#   lease has lapsed has no trustworthy rows. Both fail closed.
# - Only a canonical nodekey is accepted. The Go store validates the same
#   shape before a row is kept, so anything else here means the observation
#   is not what the contract says, and it is not pushed to the sidecar. The
#   pattern and the zero key are the forwarding policy's, which checks the
#   same shape before an expose.
# - A reason is a fixed value, never text from what failed (exposing side fix,
#   2026-11-27). The org's tailnet binding that cannot be resolved has its own,
#   TAILNET_UNRESOLVED, because it used to be reported as ROSTER_UNAVAILABLE,
#   which sends the operator to a sidecar that is answering: the roster was
#   read, and it is the binding that is not known.
# - Integration fix (2026-11-27): a @members grant is expanded to the keys of
#   every active row with a canonical key (member_keys); it never becomes
#   "allow". A row without this device's entry, or with a key that is not
#   canonical, is left out of that expansion without a report, because a
#   @members grant names nobody in particular. NOT_ORG_MEMBER and
#   DEVICES_UNAVAILABLE belong to a sub-org, whose inherited roster is cut
#   down to the devices of its own directory.
# - A roster row's owner is compared with a recipient only after
#   normalize_bare_owner (through bare_owner_or_none), so "user:bob" and
#   "bob" are one owner; a row whose owner cannot be normalized matches no
#   recipient.
"""Reading admission keys out of one membership observation."""

from __future__ import annotations

from typing import Any, Mapping

from hyprial.daemon.impl.forwarding.policy import CANONICAL_NODEKEY, ZERO_NODEKEY
from hyprial.daemon.impl.service_grants.models import bare_owner_or_none

ROSTER_UNAVAILABLE = "roster-unavailable"
MEMBER_NOT_IN_ROSTER = "member-not-in-roster"
MEMBER_WITHOUT_LOCAL_KEY = "member-without-local-key"
KEY_MALFORMED = "key-malformed"
TAILNET_UNRESOLVED = "tailnet-unresolved"
NOT_ORG_MEMBER = "not-org-member"
DEVICES_UNAVAILABLE = "org-devices-unavailable"

def tailnet_rows(observation: Mapping[str, Any] | None, tailnet_id: str) -> list[Mapping[str, Any]] | None:
    """Active member rows of ``tailnet_id``; ``None`` when the roster is unusable.

    Unusable means no observation, an observation without tailnets, no tailnet
    of that id, or a tailnet of that id whose local membership is not ready. A
    caller treats ``None`` as "this org's roster could not be read" and a list
    as "this is the whole truth".
    """

    if not isinstance(observation, Mapping):
        return None
    tailnets = observation.get("tailnets")
    if not isinstance(tailnets, list):
        return None
    rows: list[Mapping[str, Any]] = []
    found = False
    for tailnet in tailnets:
        if not isinstance(tailnet, Mapping) or tailnet.get("tailnetId") != tailnet_id:
            continue
        if tailnet.get("memberReady") is not True:
            return None
        found = True
        members = tailnet.get("members")
        if isinstance(members, list):
            rows.extend(member for member in members if isinstance(member, Mapping))
    return rows if found else None


def member_key(
    row: Mapping[str, Any], server_public: str
) -> str | None:
    """This device's entry in one roster row, or ``None`` when there is none."""

    by_server = row.get("clientPublicByServer")
    if not isinstance(by_server, Mapping):
        return None
    key = by_server.get(server_public)
    if not isinstance(key, str) or key == ZERO_NODEKEY:
        return None
    return key


def member_keys(rows: list[Mapping[str, Any]], *, server_public: str) -> tuple[str, ...]:
    """The canonical key of every active row that has an entry for this device."""

    keys: list[str] = []
    for row in rows:
        if row.get("state") != "active":
            continue
        key = member_key(row, server_public)
        if key is not None and CANONICAL_NODEKEY.fullmatch(key):
            keys.append(key)
    return tuple(keys)


def admission_keys(
    rows: list[Mapping[str, Any]] | None,
    recipients: list[tuple[str, str]],
    *,
    server_public: str,
) -> tuple[tuple[str, ...], list[tuple[tuple[str, str], str]]]:
    """Keys for ``recipients`` and, for each one that got none, the reason.

    ``recipients`` are ``(bare owner, device id)``. A recipient that resolves
    to a non-canonical key is reported as malformed rather than skipped
    silently, so the log can tell the two apart.
    """

    if rows is None:
        return (), [(recipient, ROSTER_UNAVAILABLE) for recipient in recipients]
    resolved: list[str] = []
    unresolved: list[tuple[tuple[str, str], str]] = []
    for recipient in recipients:
        owner, device_id = recipient
        row = next(
            (
                candidate
                for candidate in rows
                if candidate.get("state") == "active"
                and bare_owner_or_none(candidate.get("owner")) == owner
                and candidate.get("deviceId") == device_id
            ),
            None,
        )
        if row is None:
            unresolved.append((recipient, MEMBER_NOT_IN_ROSTER))
            continue
        key = member_key(row, server_public)
        if key is None:
            unresolved.append((recipient, MEMBER_WITHOUT_LOCAL_KEY))
        elif not CANONICAL_NODEKEY.fullmatch(key):
            unresolved.append((recipient, KEY_MALFORMED))
        else:
            resolved.append(key)
    return tuple(resolved), unresolved


__all__ = [
    "DEVICES_UNAVAILABLE",
    "KEY_MALFORMED",
    "MEMBER_NOT_IN_ROSTER",
    "NOT_ORG_MEMBER",
    "MEMBER_WITHOUT_LOCAL_KEY",
    "ROSTER_UNAVAILABLE",
    "TAILNET_UNRESOLVED",
    "admission_keys",
    "member_key",
    "member_keys",
    "tailnet_rows",
]
