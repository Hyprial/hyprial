"""``user:alice`` and ``alice`` are one owner wherever a grant meets a row.

Decision (service grant integration fix, 2026-11-27): owners are compared
only after ``normalize_bare_owner``. The grantor comes from the grant's path
and a recipient from the grant document, both already bare; a directory
device row or a sidecar roster row may spell its owner either way, so the
recipient's derivation and the exposing side's key lookup normalize the
row's owner before matching, and a row whose owner cannot be normalized
matches nothing.
"""

from __future__ import annotations

from hyprial.daemon.impl.service_grants.consumer.derive import derive_org
from hyprial.daemon.impl.service_grants.enforcement.roster import admission_keys
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


def test_a_roster_row_spelled_with_the_user_prefix_resolves_its_bare_recipient() -> None:
    server = "nodekey:" + "1" * 64
    key = "nodekey:" + "2" * 64
    row = {"state": "active", "owner": "user:bob", "deviceId": "device-b", "clientPublicByServer": {server: key}}

    keys, unresolved = admission_keys([row], [("bob", "device-b")], server_public=server)

    assert keys == (key,) and unresolved == []


def test_a_device_row_with_an_unusable_owner_matches_nothing() -> None:
    wanted, rejected = _derive("user:")

    assert wanted == ()
    assert len(rejected) == 1
