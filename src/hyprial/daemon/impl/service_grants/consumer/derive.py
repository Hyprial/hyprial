# Decision notes (service grant recipient side, 2026-11-26):
# - Derivation is a pure function of (verified grants, device rows, this
#   device, now): nothing here reads a file, a clock or a key, so every rule
#   below is testable and the periodic path can call it without side effects.
# - Which grants count is the grant documents module's one implementation
#   (effective_grants_for_recipient): the recipient is this exact
#   (owner, deviceId) or "@members". The grantor is taken from the document
#   path only (stored.grantor), never from the document body.
# - A grant is only as good as its device row: the row must exist under the
#   grantor, and it must pass the same validation as a hand-placed protected
#   record. A grant without one is rejected with a reason; it is never
#   connected to an address nobody published.
# - A derived entry carries the nine fields of a hand-placed entry and goes
#   through the same validation (CatalogEntry.from_json). A note that the
#   service-connect rules reject (private material, credentials in a URL)
#   therefore rejects the grant instead of relaxing the rule.
# - Derived devices are keyed by device id, exactly like a protected record.
#   Two rows that share a device id but differ in key, address or generation
#   name two different machines, and there is no fair way to prefer one: every
#   grant that depends on such an id is rejected until the collision is gone.
#   Rows that agree are one device whose ports are the union of its grants.
# - "auth" and "guide" are fixed English text that never carries material.
#   "auth" says whether a Key was granted and, if so, under which user-provider
#   entry it is stored and with which command it is bound to an agent. It
#   carries no label of its own because service-connect prints its own.
# - Integration fix (2026-11-27): a device row's owner is compared with the
#   grantor only after normalize_bare_owner, the one owner normalization of
#   service grants, so "user:alice" and "alice" are one owner. A row whose
#   owner cannot be normalized matches no grant.
"""Turn verified grants and device rows into service-connect entries and devices."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from hyprial.daemon.impl.service_connect.models import (
    CatalogEntry,
    ProtectedDevice,
    ServiceConnectError,
)
from hyprial.daemon.impl.service_grants.models import (
    ServiceEndpoint,
    StoredServiceGrant,
    bare_owner_or_none,
)
from hyprial.daemon.impl.service_grants.queries import effective_grants_for_recipient

DEVICE_UNKNOWN = "service-device-unknown"
DEVICE_INVALID = "service-device-invalid"
DEVICE_CONFLICT = "device-conflict"
UNNAMED = "unnamed"
ENTRY_INVALID = "entry-invalid"
NAME_CONFLICT = "name-conflict"
KEY_GRANTED = "granted"
KEY_UNAVAILABLE_STATE = "unavailable"
KEY_NONE = "none"
KEY_FIELD = "KEY"
KEY_ABSENT_TEXT = "none granted; the service admits this device by its own rules."
KEY_UNAVAILABLE_TEXT = "granted, but not available on this device; see service.grant.rejected events."


@dataclass(frozen=True, slots=True)
class Rejection:
    """One grant this device does not turn into a service, and why."""

    org: str
    grant_id: str
    grantor: str
    reason: str

    def fields(self) -> dict[str, Any]:
        """The event fields: identifiers and a fixed reason, nothing else."""

        return {
            "org": self.org,
            "grantId": self.grant_id,
            "grantor": self.grantor,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DerivedGrant:
    """One effective grant, ready to become a service-connect entry."""

    org: str
    grant_id: str
    grantor: str
    entry: CatalogEntry
    stored: StoredServiceGrant = dataclasses.field(repr=False, compare=False)

    @property
    def name(self) -> str:
        return self.entry.name

    @property
    def device_id(self) -> str:
        return self.entry.device_id

    @property
    def has_credential(self) -> bool:
        return self.stored.grant.credential is not None

    def with_key_state(self, state: str, entry_id: str | None) -> DerivedGrant:
        """The same grant with ``auth`` naming where its Key is, or why there is none.

        Service-connect prints this text after its own label ("Key:" on
        connect, "auth:" in the full guide), so it carries no label of its own.
        """

        auth = {
            KEY_GRANTED: (
                f"granted, stored as user-provider entry {entry_id} (field {KEY_FIELD}); "
                f"bind it to an agent: hyprial agent secret grant <agent> "
                f"--source user-provider --entry-id {entry_id} --field-name {KEY_FIELD} "
                f"--environment-name <ENV> --grant-id <grant-id> --revision 1"
            ),
            KEY_UNAVAILABLE_STATE: KEY_UNAVAILABLE_TEXT,
        }.get(state, KEY_ABSENT_TEXT)
        return dataclasses.replace(self, entry=dataclasses.replace(self.entry, auth=auth))


def _reject(stored: StoredServiceGrant, reason: str) -> Rejection:
    return Rejection(stored.org, stored.grant.grant_id, stored.grantor, reason)


def _device(service: ServiceEndpoint, row: Any) -> ProtectedDevice:
    return ProtectedDevice.from_json(
        {
            "deviceId": row.device_id,
            "serverPublic": row.server_public,
            "address": row.address,
            "generation": row.key_generation,
            "ports": [service.port],
            "enabled": True,
        }
    )


def _entry(
    stored: StoredServiceGrant, service: ServiceEndpoint, device: ProtectedDevice
) -> CatalogEntry:
    return CatalogEntry.from_json(
        {
            "name": service.name,
            "deviceId": device.device_id,
            "remotePort": service.port,
            "protocol": service.protocol,
            "localPort": None,
            "usage": service.note,
            "env": {},
            "auth": KEY_ABSENT_TEXT,
            "guide": f"Granted by {stored.grantor} in {stored.org} (grant {stored.grant.grant_id}).",
        }
    )


def _derive_one(
    stored: StoredServiceGrant, row: Any
) -> Rejection | tuple[DerivedGrant, ProtectedDevice]:
    service = stored.grant.service
    assert service is not None
    if service.name is None:
        return _reject(stored, UNNAMED)
    if row is None:
        return _reject(stored, DEVICE_UNKNOWN)
    try:
        device = _device(service, row)
    except ServiceConnectError:
        return _reject(stored, DEVICE_INVALID)
    try:
        entry = _entry(stored, service, device)
    except ServiceConnectError:
        return _reject(stored, ENTRY_INVALID)
    grant = DerivedGrant(
        org=stored.org,
        grant_id=stored.grant.grant_id,
        grantor=stored.grantor,
        entry=entry,
        stored=stored,
    )
    return grant, device


def derive_org(
    grants: Iterable[StoredServiceGrant],
    rows: Iterable[Any],
    *,
    owner: str,
    device_id: str,
    now_ms: int,
) -> tuple[tuple[tuple[DerivedGrant, ProtectedDevice], ...], tuple[Rejection, ...]]:
    """Derive the entries and devices one org offers this device, and what it rejects."""

    rows_by_owner = {
        (owner_of, row.device_id): row
        for row in rows
        if (owner_of := bare_owner_or_none(row.owner)) is not None
    }
    wanted: list[tuple[DerivedGrant, ProtectedDevice]] = []
    rejected: list[Rejection] = []
    for stored in effective_grants_for_recipient(
        grants, owner=owner, device_id=device_id, now_ms=now_ms
    ):
        service = stored.grant.service
        assert service is not None
        outcome = _derive_one(stored, rows_by_owner.get((stored.grantor, service.device_id)))
        if isinstance(outcome, Rejection):
            rejected.append(outcome)
        else:
            wanted.append(outcome)
    return tuple(wanted), tuple(rejected)


def assemble(
    parts: Iterable[Iterable[tuple[DerivedGrant, ProtectedDevice]]],
) -> tuple[tuple[DerivedGrant, ...], dict[str, ProtectedDevice], tuple[Rejection, ...]]:
    """Combine the orgs: contested device ids are rejected, agreeing devices merged."""

    flat = [pair for part in parts for pair in part]
    shapes: dict[str, set[tuple[object, ...]]] = {}
    for _grant, device in flat:
        shapes.setdefault(device.device_id, set()).add(_shape(device))
    items: list[DerivedGrant] = []
    rejected: list[Rejection] = []
    devices: dict[str, tuple[ProtectedDevice, set[int]]] = {}
    for grant, device in flat:
        if len(shapes[device.device_id]) > 1:
            rejected.append(Rejection(grant.org, grant.grant_id, grant.grantor, DEVICE_CONFLICT))
            continue
        items.append(grant)
        _base, ports = devices.setdefault(device.device_id, (device, set()))
        ports.add(grant.entry.remote_port)
    return (
        tuple(sorted(items, key=lambda item: (item.org, item.grant_id))),
        {key: _merged(base, ports) for key, (base, ports) in devices.items()},
        tuple(sorted(rejected, key=lambda item: (item.org, item.grant_id))),
    )


def _shape(device: ProtectedDevice) -> tuple[object, ...]:
    return (device.server_public, device.address, device.generation)


def _merged(base: ProtectedDevice, ports: set[int]) -> ProtectedDevice:
    return ProtectedDevice(
        base.device_id, base.server_public, base.address, base.generation, tuple(sorted(ports)), True
    )



__all__ = [
    "DEVICE_CONFLICT",
    "DEVICE_INVALID",
    "DEVICE_UNKNOWN",
    "ENTRY_INVALID",
    "KEY_FIELD",
    "KEY_GRANTED",
    "KEY_NONE",
    "KEY_UNAVAILABLE_STATE",
    "NAME_CONFLICT",
    "UNNAMED",
    "DerivedGrant",
    "Rejection",
    "assemble",
    "derive_org",
]
