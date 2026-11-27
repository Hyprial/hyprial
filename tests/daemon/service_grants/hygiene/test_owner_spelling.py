"""``user:alice`` and ``alice`` are one owner everywhere a grant meets a device row.

Decision (service grant integration fix, 2026-11-27): owners are compared
only after ``normalize_bare_owner``. The grantor comes from the grant's path
and is already bare; a device row may spell its owner either way, so the
recipient's derivation normalizes the row's owner before matching, and a row
whose owner cannot be normalized matches nothing.
"""

from __future__ import annotations

from hyprial.daemon.impl.service_grants.consumer.derive import derive_org
from hyprial.daemon.impl.service_grants.models import ServiceGrant, StoredServiceGrant
from tests.daemon.service_grants.consumer.support import ORG, PORT, device_row


def _grant() -> StoredServiceGrant:
    record = {
        "schemaVersion": 1,
        "grantId": "sg." + "a" * 26,
        "service": {"deviceId": "device-a", "port": PORT, "name": "magpie", "protocol": "tcp", "note": None},
        "recipient": {"owner": "bob", "deviceId": "device-b"},
        "grantedAtMs": 1,
        "expiresAtMs": None,
        "revokedAtMs": None,
        "credential": None,
    }
    return StoredServiceGrant(org=ORG, grantor="alice", grant=ServiceGrant.from_record(record))


def _derive(owner: str):
    return derive_org([_grant()], [device_row("device-a", owner)], owner="bob", device_id="device-b", now_ms=2)


def test_a_device_row_spelled_with_the_user_prefix_matches_its_bare_grantor() -> None:
    bare, _ = _derive("alice")
    prefixed, rejected = _derive("user:alice")

    assert len(bare) == 1
    assert rejected == ()
    assert [grant.stored.grant.grant_id for grant, _device in prefixed] == [bare[0][0].stored.grant.grant_id]


def test_a_device_row_with_an_unusable_owner_matches_nothing() -> None:
    wanted, rejected = _derive("user:")

    assert wanted == ()
    assert len(rejected) == 1
