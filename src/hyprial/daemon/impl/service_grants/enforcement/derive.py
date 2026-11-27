# Decision notes (service grant S4-py, 2026-11-26, coordinator ruling on the
# design-mismatch evidence):
# - Deriving a port's admission set is a pure function of this device's
#   grants, one roster observation per org, the org's effective tailnet id and
#   the clock. Nothing is stored: every push is a fresh derivation, which is
#   what lets a later maintenance tick add a device whose roster row was
#   completed after the grant was written.
# - "Effective" has one meaning, S3's is_grant_effective. This module never
#   decides effectiveness on its own.
# - A port's keys are the union over every org the device belongs to. An org
#   whose roster cannot be read contributes nothing and is reported: each of
#   its grants is listed as unresolved.
# - Integration fix (2026-11-27): a @members grant admits the members of its
#   own org and nobody else, so it is expanded to keys and the result is
#   never "allow". A port is reachable over every tailnet its device is on,
#   which is why "allow" admitted the members of every org the grantor is in.
#   The keys are those of every active row of the org's effective tailnet.
#   A sub-org inherits its parent's tailnet, whose roster also lists the
#   parent's members, so for an org in ``org_devices`` the roster is cut down
#   to the devices of the org's own directory whose owners are its members;
#   a recipient outside them is unresolved as NOT_ORG_MEMBER, and an org
#   whose directory could not be read (``None``) contributes nothing, each of
#   its grants unresolved as DEVICES_UNAVAILABLE. Over the sidecar's key
#   limit the caller's existing limit path applies.
# - A grant counts only when it was issued by this device for this device's
#   port. Another grantor's grant is not this device's to push.
# - A port this device has issued a grant for stays in the result after that
#   grant is revoked or expires, with an empty set. Only a port with no grant
#   document at all is absent; the caller pushes nobody for both.
# - An org whose tailnet binding could not be resolved (S4-py fix, 2026-11-27,
#   coordinator ruling) contributes nothing, as an org whose roster cannot be
#   read does not, and each of its grants is listed under its own reason. It
#   has no tailnet id to read rows by, which is why it is named apart: to the
#   derivation it looks like an unreadable roster, and to the operator it is
#   not one.
"""Deriving each port's admission set from grants and the sidecar roster."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..models import ALL_MEMBERS, GrantRecipient, StoredServiceGrant, normalize_bare_owner
from ..queries import is_grant_effective
from .models import AdmissionProjection, AdmissionSet, UnresolvedGrant
from .roster import (
    DEVICES_UNAVAILABLE,
    NOT_ORG_MEMBER,
    ROSTER_UNAVAILABLE,
    TAILNET_UNRESOLVED,
    admission_keys,
    member_keys,
    tailnet_rows,
)

OrgDevices = frozenset[tuple[str, str]]


@dataclass(frozen=True)
class _Pushable:
    """One effective grant this device issued for one of its ports."""

    org: str
    grant_id: str
    port: int
    recipient: GrantRecipient | None


def _pushable(
    grants_by_org: Mapping[str, Sequence[StoredServiceGrant]],
    *,
    owner: str,
    device_id: str,
    now_ms: int,
) -> tuple[list[_Pushable], set[int]]:
    """This device's effective grants, and every port it has issued a grant for.

    ``owner`` may be bare or ``user:<owner>``, matched the way S3 matches it.
    """

    bare_owner = normalize_bare_owner(owner)
    pushable: list[_Pushable] = []
    granted_ports: set[int] = set()
    for org, grants in grants_by_org.items():
        for item in grants:
            grant = item.grant
            service = grant.service
            if service is None or service.device_id != device_id or item.grantor != bare_owner:
                continue
            if type(service.port) is int:
                granted_ports.add(service.port)
            if not is_grant_effective(grant, now_ms=now_ms):
                continue
            recipient = grant.recipient
            pushable.append(
                _Pushable(
                    org=org,
                    grant_id=grant.grant_id,
                    port=service.port,
                    recipient=None if recipient == ALL_MEMBERS else recipient,
                )
            )
    return pushable, granted_ports


def _recipient_view(recipient: GrantRecipient | None) -> dict[str, str]:
    if recipient is None:
        return {"recipient": ALL_MEMBERS}
    return {"owner": recipient.owner, "deviceId": recipient.device_id}


def _in_org(rows: list[Mapping[str, Any]], devices: OrgDevices) -> list[Mapping[str, Any]]:
    return [
        row
        for row in rows
        if isinstance(row.get("owner"), str)
        and (normalize_bare_owner(row["owner"]), row.get("deviceId")) in devices
    ]


def derive_admission(
    grants_by_org: Mapping[str, Sequence[StoredServiceGrant]],
    rosters: Mapping[str, Mapping[str, Any] | None],
    tailnet_ids: Mapping[str, str],
    *,
    owner: str,
    device_id: str,
    server_public: str,
    now_ms: int,
    tailnets_unresolved: Sequence[str] = (),
    org_devices: Mapping[str, OrgDevices | None] | None = None,
) -> dict[int, AdmissionProjection]:
    """Admission to push for each port this device has a grant for.

    ``grants_by_org[org]`` are the stored grants of ``org``, of any grantor and
    port; the ones that are not this device's are filtered out here.
    ``rosters[org]`` is the membership observation to read ``org``'s members
    from, or ``None`` when it could not be read. ``tailnet_ids[org]`` is the
    org's effective tailnet id. ``tailnets_unresolved`` names the orgs whose
    binding could not be resolved, which therefore have no entry in
    ``tailnet_ids``; each of their effective grants is reported as
    unresolved under ``TAILNET_UNRESOLVED`` and none of them is looked up in
    a roster. ``org_devices[org]``, given for an org that inherits its
    tailnet, is the ``(bare owner, device id)`` of every device of the org's
    own directory whose owner is a member, or ``None`` when that directory
    could not be read. Every set is ``"keys"``: a ``@members`` grant adds the
    key of every active member row. A port this device never issued a grant
    for is absent from the result; its caller pushes an empty key set. A
    port whose grants are all revoked or expired is present with an empty
    key set.
    """

    restricted = org_devices or {}
    keys: dict[int, set[str]] = {}
    unresolved: dict[int, list[UnresolvedGrant]] = {}

    def report(item: _Pushable, reason: str) -> None:
        unresolved.setdefault(item.port, []).append(
            UnresolvedGrant(
                org=item.org,
                grant_id=item.grant_id,
                recipient=_recipient_view(item.recipient),
                reason=reason,
            )
        )

    def members_of(org: str) -> list[Mapping[str, Any]] | None:
        tailnet_id = tailnet_ids.get(org)
        rows = None if tailnet_id is None else tailnet_rows(rosters.get(org), tailnet_id)
        devices = restricted.get(org)
        if rows is None or org not in restricted or devices is None:
            return rows
        return _in_org(rows, devices)

    pushable, granted_ports = _pushable(
        grants_by_org, owner=owner, device_id=device_id, now_ms=now_ms
    )
    for item in pushable:
        if item.org in tailnets_unresolved:
            report(item, TAILNET_UNRESOLVED)
            continue
        if item.org in restricted and restricted[item.org] is None:
            report(item, DEVICES_UNAVAILABLE)
            continue
        members = members_of(item.org)
        if item.recipient is None:
            if members is None:
                report(item, ROSTER_UNAVAILABLE)
            else:
                keys.setdefault(item.port, set()).update(
                    member_keys(members, server_public=server_public)
                )
            continue
        devices = restricted.get(item.org)
        if (
            members is not None
            and devices is not None
            and (item.recipient.owner, item.recipient.device_id) not in devices
        ):
            report(item, NOT_ORG_MEMBER)
            continue
        resolved, missing = admission_keys(
            members,
            [(item.recipient.owner, item.recipient.device_id)],
            server_public=server_public,
        )
        keys.setdefault(item.port, set()).update(resolved)
        for _recipient, reason in missing:
            report(item, reason)

    ports = set(keys) | set(unresolved) | granted_ports
    return {
        port: AdmissionProjection(
            admission=AdmissionSet(admission="keys", keys=tuple(sorted(keys.get(port, ())))),
            unresolved=tuple(_stable(unresolved.get(port, ()))),
        )
        for port in sorted(ports)
    }


def _stable(items: Iterable[UnresolvedGrant]) -> list[UnresolvedGrant]:
    """A deterministic order that does not depend on org or grant order."""

    def sort_key(item: UnresolvedGrant) -> tuple[str, ...]:
        recipient = item.recipient
        return (
            item.org,
            item.grant_id,
            recipient.get("owner", ""),
            recipient.get("deviceId", ""),
            item.reason,
        )

    return sorted(items, key=sort_key)


__all__ = ["derive_admission"]
