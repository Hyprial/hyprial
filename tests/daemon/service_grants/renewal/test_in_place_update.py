"""Renewing a grant, or changing its Key, updates the effective grant in place.

Decision (service grant integration fix, 2026-11-27): when ``service.grant``
finds an effective grant for the same (local device, port, recipient), it
rewrites that document under the same ``grantId``: credential, expiry, name,
protocol and note change, ``grantedAtMs`` keeps its first value and
``updatedAtMs`` is the time of the rewrite. A new ``grantId`` is issued only
when there is no effective grant. The reply is ``{grant, updated}``; a first
grant emits ``service.grant.created`` and an update ``service.grant.updated``
with the same fields.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from tests.daemon.service_grants.consumer.support import write_user_provider
from tests.daemon.service_grants.renewal._world import (
    BOB_NODE,
    CAROL_NODE,
    DAY_MS,
    MEMBERS,
    ORG,
    PORT,
    ROTATED,
    World,
)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    made = World(tmp_path)
    for member in MEMBERS:
        made.publish(member)
    try:
        yield made
    finally:
        made.close()


def test_a_second_grant_to_the_same_recipient_rewrites_the_first_in_place(world: World) -> None:
    first = world.grants().grant(
        org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie", note="first"
    )
    granted_at = world.clock[0]
    world.clock[0] += 1000
    second = world.grants().grant(
        org=ORG,
        port=PORT,
        device=BOB_NODE.slug,
        name="magpie-two",
        protocol="http",
        note="second",
        expires_at_ms=world.clock[0] + DAY_MS,
    )

    assert first["updated"] is False
    assert first["grant"]["updatedAtMs"] is None
    assert "superseded" not in first and "superseded" not in second
    assert second["updated"] is True
    view = second["grant"]
    assert view["grantId"] == first["grant"]["grantId"]
    assert view["grantedAtMs"] == granted_at
    assert view["updatedAtMs"] == world.clock[0]
    assert view["expiresAtMs"] == world.clock[0] + DAY_MS
    assert view["service"]["name"] == "magpie-two"
    assert view["service"]["protocol"] == "http"
    assert view["service"]["note"] == "second"
    [doc] = world.grant_docs()
    assert doc["grantId"] == view["grantId"]
    assert doc["grantedAtMs"] == granted_at
    assert doc["updatedAtMs"] == world.clock[0]
    assert doc["revokedAtMs"] is None


def test_an_update_emits_updated_with_the_fields_of_created(world: World) -> None:
    world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")
    world.clock[0] += 1
    world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")

    [created] = world.events_of("service.grant.created")
    [updated] = world.events_of("service.grant.updated")
    assert "superseded" not in created
    assert set(updated) == set(created)
    assert updated["grantId"] == created["grantId"]
    assert world.events_of("service.grant.revoked") == []


def test_a_new_key_is_sealed_into_the_same_grant(world: World) -> None:
    first = world.grants().grant(
        org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie",
        credential={"entryId": "gateway", "fieldName": "API_KEY"},
    )
    [before] = world.grant_docs()
    write_user_provider(world.homes["alice"], "gateway", {"API_KEY": ROTATED})
    world.clock[0] += 1
    second = world.grants().grant(
        org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie",
        credential={"entryId": "gateway", "fieldName": "API_KEY"},
    )

    assert second["updated"] is True
    assert second["grant"]["grantId"] == first["grant"]["grantId"]
    assert second["grant"]["hasCredential"] is True
    [after] = world.grant_docs()
    assert after["credential"]["payload"] != before["credential"]["payload"]
    assert after["credential"]["header"]["grantId"] == first["grant"]["grantId"]


def test_an_update_may_clear_the_key(world: World) -> None:
    world.grants().grant(
        org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie",
        credential={"entryId": "gateway", "fieldName": "API_KEY"},
    )
    world.clock[0] += 1
    second = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")

    assert second["updated"] is True
    assert second["grant"]["hasCredential"] is False
    [doc] = world.grant_docs()
    assert doc["credential"] is None


def test_other_recipients_keep_their_own_grants(world: World) -> None:
    to_bob = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")
    to_carol = world.grants().grant(org=ORG, port=PORT, device=CAROL_NODE.slug, name="magpie")
    to_members = world.grants().grant(org=ORG, port=PORT, device="@members", name="magpie")

    assert {to_bob["updated"], to_carol["updated"], to_members["updated"]} == {False}
    assert len({to_bob["grant"]["grantId"], to_carol["grant"]["grantId"], to_members["grant"]["grantId"]}) == 3
    again = world.grants().grant(org=ORG, port=PORT, device="@members", name="magpie")
    assert again["updated"] is True
    assert again["grant"]["grantId"] == to_members["grant"]["grantId"]


def test_a_revoked_grant_is_not_updated(world: World) -> None:
    first = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")
    world.grants().revoke(org=ORG, grant_id=first["grant"]["grantId"])
    world.clock[0] += 1
    second = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")

    assert second["updated"] is False
    assert second["grant"]["grantId"] != first["grant"]["grantId"]
    assert second["grant"]["updatedAtMs"] is None


def test_an_expired_grant_is_not_updated(world: World) -> None:
    first = world.grants().grant(
        org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie",
        expires_at_ms=world.clock[0] + 10,
    )
    world.clock[0] += 10
    second = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")

    assert second["updated"] is False
    assert second["grant"]["grantId"] != first["grant"]["grantId"]
    expired = next(doc for doc in world.grant_docs() if doc["grantId"] == first["grant"]["grantId"])
    assert expired["revokedAtMs"] is None
    assert expired["updatedAtMs"] is None


def test_the_listing_shows_updated_at(world: World) -> None:
    first = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")
    world.clock[0] += 5
    world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")

    [granted] = world.grants().grants(org=ORG)["granted"]
    assert granted["grantId"] == first["grant"]["grantId"]
    assert granted["updatedAtMs"] == world.clock[0]
    [received] = world.grants(BOB_NODE).grants(org=ORG)["received"]
    assert received["updatedAtMs"] == world.clock[0]
