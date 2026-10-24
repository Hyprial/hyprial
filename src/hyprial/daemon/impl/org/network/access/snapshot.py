"""Who this node dials and lets in, read once from the org directory (S2b, §5).

One read of the directory spaces gives everything the network side needs:

- ``peers``: the other member devices (label -> Tailcat address), plus the
  invite-authorized bootstrap peers the directory does not name yet;
- ``peer_orgs`` / ``added``: the ordering inputs of the one-dialer rule;
- ``allow_keys``: only the per-server derived client nodekeys that the sidecar
  admits inbound; master client keys and keys derived for another server never
  confer authority;
- ``allow_any``: always false. Protocol v5 admission is fail-closed and has no
  owner-wide compatibility exception.
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
import hashlib
from pathlib import Path
import re
from typing import Any

from hyprial.daemon.impl.org.network.binding import resolve_org_directory_space
from hyprial.daemon.impl.org.network.state import (
    read_bootstrap_peers,
    remove_bootstrap_peers,
)
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import read_device_record
from hyprial.kernel import canonical_user_uri


@dataclass(frozen=True)
class OrgAccess:
    """One org's contribution: what a rebuild read from that org alone."""

    space_id: str
    owned: bool
    added: Mapping[str, datetime]
    devices: Mapping[
        tuple[str, str], str
    ]  # other members' devices -> address ("" if none)
    client_keys: Mapping[tuple[str, str], str]
    machine_fingerprints: Mapping[tuple[str, str], str | None]
    first_published: Mapping[tuple[str, str], datetime | None]
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
    denied_peer_labels: frozenset[str] = frozenset()
    denied_address_hashes: frozenset[str] = frozenset()
    reissue_reason: str | None = None
    reissue_token: str | None = None

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
        service._log(
            "warn", "org", "org.directory_peers.failed", org=org, detail=str(error)
        )
        return None
    names = {member.user.removeprefix("user:") for member in members}
    current = [
        device for device in devices if device.owner.removeprefix("user:") in names
    ]
    if service._owner.removeprefix("user:") not in names:
        current = []
    return space.space_id, space, members, current


def _address_hash(address: str) -> str:
    return hashlib.sha256(address.encode("utf-8")).hexdigest()


def _with_bootstrap(
    service: Any,
    rows: dict[tuple[str, str], str],
    *,
    denied_labels: frozenset[str] = frozenset(),
    denied_address_hashes: frozenset[str] = frozenset(),
) -> dict[str, str]:
    peers = service._render_directory_peers(rows)
    named = {service._peer_label(owner, device_id) for owner, device_id in rows}
    bootstrap = read_bootstrap_peers(service._home)
    shadowed = [peer for peer in bootstrap if peer in named]
    if shadowed:
        remove_bootstrap_peers(service._home, shadowed)
    for peer, entry in bootstrap.items():
        identity = (entry["inviter"], entry["deviceId"])
        if (
            peer in named
            or peer in denied_labels
            or _address_hash(entry["address"]) in denied_address_hashes
            or identity == (service._owner, service._node_id)
        ):
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


def _derived_admission_key(device: Any, local_server_public: str) -> str | None:
    """Return this server's valid derived key, or reject the whole row."""

    derived = getattr(device, "client_public_by_server", None)
    if not isinstance(derived, Mapping):
        return None
    values = tuple(derived.values())
    if (
        not all(isinstance(value, str) and is_admissible_key(value) for value in values)
        or len(set(values)) != len(values)
        or device.client_public in values
    ):
        return None
    key = derived.get(local_server_public)
    return key if isinstance(key, str) and is_admissible_key(key) else None


def _org_access(
    service: Any, org: str, local_server_public: str
) -> OrgAccess | None:
    view = _org_view(service, org)
    if view is None:
        return None
    space_id, space, members, current = view
    devices: dict[tuple[str, str], str] = {}
    client_keys: dict[tuple[str, str], str] = {}
    machine_fingerprints: dict[tuple[str, str], str | None] = {}
    first_published: dict[tuple[str, str], datetime | None] = {}
    keys: set[str] = set()
    for device in current:
        identity = (device.owner.removeprefix("user:"), device.device_id)
        if device.address and not devices.get(identity):
            devices[identity] = device.address
        else:
            devices.setdefault(identity, "")
        admission_key = _derived_admission_key(device, local_server_public)
        if admission_key is not None:
            client_keys[identity] = admission_key
            keys.add(admission_key)
        elif getattr(device, "client_public_by_server", None):
            service._log(
                "warn",
                "org",
                "org.access.invalid-key",
                org=org,
                peer=service._peer_label(*identity),
                detail="derived client key row is invalid for this server",
            )
        machine_fingerprints[identity] = device.machine_fingerprint
        first_published[identity] = _added_at(device.first_published_at)
    return OrgAccess(
        space_id=space_id,
        owned=space.owner == canonical_user_uri(service._owner),
        added={
            member.user.removeprefix("user:"): when
            for member in members
            if (when := _added_at(member.added_at)) is not None
        },
        devices=devices,
        client_keys=client_keys,
        machine_fingerprints=machine_fingerprints,
        first_published=first_published,
        keys=frozenset(keys),
    )


def _resolve_address_collisions(
    service: Any,
    rows: Mapping[tuple[str, str], str],
    fingerprints: Mapping[tuple[str, str], str | None],
    first_published: Mapping[tuple[str, str], datetime | None],
    own: tuple[str, str],
) -> tuple[frozenset[tuple[str, str]], frozenset[str], str | None]:
    """Return denied identities/address hashes and a local reissue token."""

    identities_by_address: dict[str, list[tuple[str, str]]] = {}
    for identity, address in rows.items():
        if address:
            identities_by_address.setdefault(address, []).append(identity)
    denied: set[tuple[str, str]] = set()
    denied_address_hashes: set[str] = set()
    reissue_token: str | None = None
    for address, identities in sorted(identities_by_address.items()):
        identities = sorted(set(identities))
        if len(identities) < 2:
            continue
        denied.update(identities)
        digest = _address_hash(address)
        denied_address_hashes.add(digest)
        origins = [fingerprints.get(identity) for identity in identities]
        times = [first_published.get(identity) for identity in identities]
        later: tuple[str, str] | None = None
        if (
            all(origins)
            and len(set(origins)) == len(origins)
            and all(times)
            and len(set(times)) == len(times)
        ):
            later = max(identities, key=lambda identity: first_published[identity])
            if later == own:
                reissue_token = digest
        service._log(
            "error",
            "org",
            "org.access.address-collision",
            addressHash=digest[:12],
            deviceIds=sorted(device_id for _owner, device_id in identities),
            resolution="reissue-later" if later is not None else "manual",
            **({"reissueDeviceId": later[1]} if later is not None else {}),
        )
    return frozenset(denied), frozenset(denied_address_hashes), reissue_token


def build_access_snapshot(
    service: Any, carry: Mapping[str, OrgAccess] | None = None
) -> AccessSnapshot:
    """Read every org once; an unreadable org contributes ``carry[org]`` if given."""

    own = (service._owner, service._node_id)
    live_server_public = getattr(service, "_local_server_public", None)
    local_server_public = live_server_public() if callable(live_server_public) else None
    if not local_server_public:
        record = read_device_record(service._home)
        local_server_public = record.server_public if record is not None else ""
    orgs: dict[str, OrgAccess] = {}
    unreadable: list[str] = []
    for org in service._orgs():
        part = _org_access(service, org, local_server_public)
        if part is None:
            unreadable.append(org)
            part = (carry or {}).get(org)
        if part is not None:
            orgs[org] = part
    rows: dict[tuple[str, str], str] = {}
    claims: dict[str, set[tuple[str, str]]] = {}
    fingerprints: dict[tuple[str, str], str | None] = {}
    first_published: dict[tuple[str, str], datetime | None] = {}
    peer_orgs: dict[str, set[str]] = {}
    for org, part in orgs.items():
        for identity, address in part.devices.items():
            if address:
                rows.setdefault(identity, address)
            admission_key = part.client_keys.get(identity)
            if admission_key is not None:
                claims.setdefault(admission_key, set()).add(identity)
            fingerprints.setdefault(identity, part.machine_fingerprints[identity])
            first_published.setdefault(identity, part.first_published[identity])
            if identity != own:
                peer_orgs.setdefault(service._peer_label(*identity), set()).add(org)
    denied, denied_address_hashes, reissue_token = _resolve_address_collisions(
        service, rows, fingerprints, first_published, own
    )
    safe_rows = {
        identity: address
        for identity, address in rows.items()
        if identity != own and identity not in denied
    }
    denied_labels = frozenset(service._peer_label(*identity) for identity in denied)
    allow_keys = {
        key
        for key, identities in claims.items()
        if len(identities) == 1
        and own not in identities
        and not identities.intersection(denied)
    }
    for label in denied_labels:
        peer_orgs.pop(label, None)
    return AccessSnapshot(
        own=own,
        peers=_with_bootstrap(
            service,
            safe_rows,
            denied_labels=denied_labels,
            denied_address_hashes=denied_address_hashes,
        ),
        peer_orgs=peer_orgs,
        added={org: part.added for org, part in orgs.items()},
        allow_keys=() if unreadable else tuple(sorted(allow_keys)),
        allow_any=False,
        spaces=tuple((org, part.space_id) for org, part in orgs.items()),
        orgs=orgs,
        unreadable=tuple(unreadable),
        denied_peer_labels=denied_labels,
        denied_address_hashes=denied_address_hashes,
        reissue_reason="directory-conflict" if reissue_token is not None else None,
        reissue_token=reissue_token,
    )


def with_bootstrap(snapshot: AccessSnapshot, home: Path) -> AccessSnapshot:
    """Add the bootstrap peers written since the last rebuild (dial only).

    ``org join`` writes the inviter as a bootstrap peer and redials at once,
    before any directory event: without this, that redial reads a snapshot
    without the inviter and unmaps it, and join-org finds no holder.  One
    small local file per tick; no directory read. Collision-denied labels and
    address hashes remain authoritative across this late merge.
    """

    extra = {
        peer: entry["address"]
        for peer, entry in read_bootstrap_peers(home).items()
        if peer not in snapshot.peers
        and peer not in snapshot.denied_peer_labels
        and _address_hash(entry["address"]) not in snapshot.denied_address_hashes
        and (entry["inviter"], entry["deviceId"]) != snapshot.own
    }
    return replace(snapshot, peers={**snapshot.peers, **extra}) if extra else snapshot
