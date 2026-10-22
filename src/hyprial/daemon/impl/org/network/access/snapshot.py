"""Who this node dials and lets in, read once from the org directory (S2b, §5).

One read of the directory spaces gives everything the network side needs:

- ``peers``: the other member devices (label -> Tailcat address), plus the
  invite-authorized bootstrap peers the directory does not name yet;
- ``peer_orgs`` / ``added``: the ordering inputs of the one-dialer rule;
- ``allow_keys``: the client nodekeys the sidecar admits inbound;
- ``allow_any``: True on every device of a user who owns an org, for all of
  that node's orgs.  ``LAX(join-endpoint)``: until the owner's join endpoint
  exists, a joiner's key is unknown before its row syncs (org-improver,
  2026-10-08; the join endpoint is a separate PAC and must ship in 0.5.5; it
  flips ``test_an_org_owner_admits_every_key_until_the_join_endpoint``).
- ``orgs``: each org's own contribution (``OrgAccess``), so one unreadable
  org can be carried forward without freezing the others;
- ``unreadable`` / ``incomplete``: orgs that could not be read this time.
  The caller may pass their previous contributions as ``carry``; every
  readable org is recomputed, so a removal there still revokes.  The actor
  bounds how long a contribution is carried (``CARRY_LIMIT_S``).

A client key enters the allow set only as ``nodekey:`` plus 64 hex digits.
The sidecar rejects a whole ``allow`` that holds one malformed key, which
would freeze revocation on every member node; a bad key is dropped and
logged instead.

Only rows of **current** members count, read from the same member table the
allow set and the dial order use.  The OrgFS projection already hides a
removed member's rows (checked on a real runtime, 2026-10-08); the filter is
defence in depth so the three sets cannot disagree.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
import re
from typing import Any

from hyprial.daemon.impl.org.network.binding import resolve_org_directory_space
from hyprial.daemon.impl.org.network.state import read_bootstrap_peers, remove_bootstrap_peers
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.kernel import canonical_user_uri


@dataclass(frozen=True)
class OrgAccess:
    """One org's contribution: what a rebuild read from that org alone."""

    space_id: str
    owned: bool
    added: Mapping[str, datetime]
    devices: Mapping[tuple[str, str], str]  # other members' devices -> address ("" if none)
    keys: frozenset[str]


@dataclass(frozen=True)
class AccessSnapshot:
    own: tuple[str, str]
    peers: Mapping[str, str]
    peer_orgs: Mapping[str, set[str]]
    added: Mapping[str, Mapping[str, datetime]]
    allow_keys: tuple[str, ...]
    allow_any: bool
    spaces: tuple[tuple[str, str], ...] = field(default=())
    orgs: Mapping[str, OrgAccess] = field(default_factory=dict)
    unreadable: tuple[str, ...] = ()

    @property
    def allow(self) -> tuple[tuple[str, ...], bool]:
        return self.allow_keys, self.allow_any

    @property
    def incomplete(self) -> bool:
        return bool(self.unreadable)


_NODEKEY = re.compile(r"nodekey:[0-9a-f]{64}")


def is_admissible_key(key: str) -> bool:
    return _NODEKEY.fullmatch(key) is not None


def _added_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _org_view(service: Any, org: str) -> tuple[str, Any, list[Any], list[Any]] | None:
    """``(space_id, space, members, current members' devices)`` or None."""

    try:
        space, _reason = resolve_org_directory_space(service._fs, org)
        if space is None:
            return None
        members = list(service._fs.members(space.space_id))
        devices = service._store.list_devices(org)
    except (OrgFsError, ValueError) as error:
        service._log("warn", "org", "org.directory_peers.failed", org=org, detail=str(error))
        return None
    names = {member.user.removeprefix("user:") for member in members}
    current = [device for device in devices if device.owner.removeprefix("user:") in names]
    return space.space_id, space, members, current


def _with_bootstrap(service: Any, rows: dict[tuple[str, str], str]) -> dict[str, str]:
    peers = service._render_directory_peers(rows)
    named = {service._peer_label(owner, device_id) for owner, device_id in rows}
    bootstrap = read_bootstrap_peers(service._home)
    shadowed = [peer for peer in bootstrap if peer in named]
    if shadowed:
        remove_bootstrap_peers(service._home, shadowed)
    for peer, entry in bootstrap.items():
        identity = (entry["inviter"], entry["deviceId"])
        if peer in named or identity == (service._owner, service._node_id):
            continue
        peers[peer] = entry["address"]
    return peers


def directory_peers(service: Any) -> dict[str, str]:
    """Stable ``owner/deviceId`` -> address for every other current member device.

    Directory first, then the invite-authorized bootstrap peers the directory
    does not name yet (§2.5: a joining node's directory is empty until sync).
    A bootstrap entry the directory has caught up with is pruned.
    """

    return build_access_snapshot(service).peers  # type: ignore[return-value]


def _org_access(service: Any, org: str, own: tuple[str, str]) -> OrgAccess | None:
    view = _org_view(service, org)
    if view is None:
        return None
    space_id, space, members, current = view
    devices: dict[tuple[str, str], str] = {}
    keys: set[str] = set()
    for device in current:
        identity = (device.owner.removeprefix("user:"), device.device_id)
        if identity == own:
            continue
        if device.address and not devices.get(identity):
            devices[identity] = device.address
        else:
            devices.setdefault(identity, "")
        if is_admissible_key(device.client_public):
            keys.add(device.client_public)
        elif device.client_public:
            service._log(
                "warn", "org", "org.access.invalid-key",
                org=org, peer=service._peer_label(*identity),
                detail="client key is not nodekey:<64 hex>",
            )
    return OrgAccess(
        space_id=space_id,
        owned=space.owner == canonical_user_uri(service._owner),
        added={
            member.user.removeprefix("user:"): when
            for member in members
            if (when := _added_at(member.added_at)) is not None
        },
        devices=devices,
        keys=frozenset(keys),
    )


def build_access_snapshot(service: Any, carry: Mapping[str, OrgAccess] | None = None) -> AccessSnapshot:
    """Read every org once; an unreadable org contributes ``carry[org]`` if given."""

    own = (service._owner, service._node_id)
    orgs: dict[str, OrgAccess] = {}
    unreadable: list[str] = []
    for org in service._orgs():
        part = _org_access(service, org, own)
        if part is None:
            unreadable.append(org)
            part = (carry or {}).get(org)
        if part is not None:
            orgs[org] = part
    rows: dict[tuple[str, str], str] = {}
    peer_orgs: dict[str, set[str]] = {}
    for org, part in orgs.items():
        for identity, address in part.devices.items():
            peer_orgs.setdefault(service._peer_label(*identity), set()).add(org)
            if address:
                rows.setdefault(identity, address)
    return AccessSnapshot(
        own=own,
        peers=_with_bootstrap(service, rows),
        peer_orgs=peer_orgs,
        added={org: part.added for org, part in orgs.items()},
        allow_keys=tuple(sorted(set().union(*(part.keys for part in orgs.values())))),
        allow_any=any(part.owned for part in orgs.values()),
        spaces=tuple((org, part.space_id) for org, part in orgs.items()),
        orgs=orgs,
        unreadable=tuple(unreadable),
    )


def with_bootstrap(snapshot: AccessSnapshot, home: Path) -> AccessSnapshot:
    """Add the bootstrap peers written since the last rebuild (dial only).

    ``org join`` writes the inviter as a bootstrap peer and redials at once,
    before any directory event: without this, that redial reads a snapshot
    without the inviter and unmaps it, and join-org finds no holder.  One
    small local file per tick; no directory read.
    """

    extra = {
        peer: entry["address"]
        for peer, entry in read_bootstrap_peers(home).items()
        if peer not in snapshot.peers and (entry["inviter"], entry["deviceId"]) != snapshot.own
    }
    return replace(snapshot, peers={**snapshot.peers, **extra}) if extra else snapshot
