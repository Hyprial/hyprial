"""The recipient says once, and why, when a grant stops giving it something.

Decision (service grant integration fix, 2026-11-27): when the recipient
withdraws a grant's Key entry, or a grant leaves what it offers
service-connect (whose mapping then comes down), it emits
``service.grant.withdrawn`` with ``org``, ``grantId``, ``grantor`` and a fixed
``reason``: revoked, expired, credential-cleared, grantor-device-gone,
device-conflict, left-org, not-org-member, org-deleted, grant-removed, or the
fixed rejection reason that now stops the grant. One event per grant per
withdrawal; a steady tick emits nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.service_grants.consumer import GrantConsumer
from tests.daemon.service_grants.consumer.support import ORG, PORT, grantor, leave_org
from tests.daemon.service_grants.consumer_fixes.support import close, consumer_arguments, world_at

WITHDRAWN = "service.grant.withdrawn"
CREDENTIAL = {"entryId": "gateway", "fieldName": "API_KEY"}


@pytest.fixture
def world(tmp_path: Path, actor_host) -> dict[str, Any]:
    made = world_at(tmp_path, actor_host)
    yield made
    close(made)


def _grant(world: dict[str, Any], **extra: Any) -> str:
    side = grantor(world, world["clock"])
    arguments = {"org": ORG, "port": PORT, "device": "bob/device-b", "name": "magpie", **extra}
    return side.grant(**arguments)["grant"]["grantId"]


def _started(world: dict[str, Any], **extra: Any) -> tuple[GrantConsumer, str]:
    grant_id = _grant(world, **extra)
    source = GrantConsumer(**consumer_arguments(world))
    source.refresh()
    assert [grant.grant_id for grant in source._snapshot.grants] == [grant_id]
    assert world["events"].of(WITHDRAWN) == []
    return source, grant_id


def _withdrawn(world: dict[str, Any], source: GrantConsumer, grant_id: str, reason: str) -> None:
    source.refresh()
    source.refresh()
    assert world["events"].of(WITHDRAWN) == [
        {"org": ORG, "grantId": grant_id, "grantor": "alice", "reason": reason}
    ]


def test_a_revoked_grant_with_a_key_is_withdrawn_once_as_revoked(world: dict[str, Any]) -> None:
    source, grant_id = _started(world, credential=CREDENTIAL)
    assert world["events"].of("service.grant.received")

    grantor(world, world["clock"]).revoke(org=ORG, grant_id=grant_id)

    _withdrawn(world, source, grant_id, "revoked")


def test_a_revoked_grant_without_a_key_is_withdrawn_as_revoked(world: dict[str, Any]) -> None:
    source, grant_id = _started(world)

    grantor(world, world["clock"]).revoke(org=ORG, grant_id=grant_id)

    _withdrawn(world, source, grant_id, "revoked")


def test_an_expired_grant_is_withdrawn_as_expired(world: dict[str, Any]) -> None:
    source, grant_id = _started(world, expires_at_ms=world["clock"].value + 1_000)

    world["clock"].value += 2_000

    _withdrawn(world, source, grant_id, "expired")


def test_a_key_cleared_by_an_in_place_update_is_withdrawn_as_credential_cleared(world: dict[str, Any]) -> None:
    source, grant_id = _started(world, credential=CREDENTIAL)

    assert _grant(world) == grant_id

    _withdrawn(world, source, grant_id, "credential-cleared")
    assert [grant.grant_id for grant in source._snapshot.grants] == [grant_id]


def test_a_grantor_device_that_is_gone_withdraws_as_grantor_device_gone(world: dict[str, Any]) -> None:
    source, grant_id = _started(world)

    world["store"].remove_device(ORG, "device-a", owner="alice")

    _withdrawn(world, source, grant_id, "grantor-device-gone")


def test_a_contested_device_withdraws_as_device_conflict(world: dict[str, Any]) -> None:
    source, grant_id = _started(world)

    source.contested(("device-a",))

    _withdrawn(world, source, grant_id, "device-conflict")


def test_leaving_the_org_withdraws_as_left_org(world: dict[str, Any]) -> None:
    source, grant_id = _started(world, credential=CREDENTIAL)

    leave_org(world["bob_home"])

    _withdrawn(world, source, grant_id, "left-org")


def test_a_steady_tick_emits_no_withdrawal(world: dict[str, Any]) -> None:
    source, _grant_id = _started(world, credential=CREDENTIAL)

    for _ in range(3):
        source.refresh()

    assert world["events"].of(WITHDRAWN) == []
