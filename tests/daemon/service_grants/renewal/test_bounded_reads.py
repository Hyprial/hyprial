"""Both ends read a grantor's documents within the same bounds.

Decision (service grant integration fix, 2026-11-27): the exposing side's
read of its own grants uses the recipient's bounded scan. A grantor directory
over a limit is unreadable: it contributes no key and is reported once as
``service.grant.directory_unavailable`` with step ``grants-limited``. On the
recipient's side ``service.grants`` reads with the same bounds, leaves an
over-limit grantor's grants out of ``received`` and names that grantor in
``limitedGrantors``, so the listing never claims "no grant" for what the
daemon could not read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from hyprial.daemon.impl.service_grants.enforcement import reader
from tests.daemon.service_grants.renewal._world import (
    BOB_NODE,
    MEMBERS,
    ORG,
    PORT,
    World,
)

LIMIT = 256


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    made = World(tmp_path)
    for member in MEMBERS:
        made.publish(member)
    try:
        yield made
    finally:
        made.close()


def test_the_exposing_side_treats_an_over_limit_own_directory_as_unreadable(world: World) -> None:
    world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")
    named: list[tuple[str, str]] = []

    within = reader.own_grants(world.store, [ORG], owner="alice", unreadable=lambda *pair: named.append(pair))
    assert len(within[ORG]) == 1 and named == []

    world.fill("alice", LIMIT, revoked_at_ms=world.clock[0])
    over = reader.own_grants(world.store, [ORG], owner="alice", unreadable=lambda *pair: named.append(pair))

    assert over == {}
    assert named == [(ORG, "grants-limited")]
    assert reader.GRANTS_LIMITED == "grants-limited"


def test_received_names_an_over_limit_grantor_instead_of_listing_nothing(world: World) -> None:
    world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")
    assert len(world.grants(BOB_NODE).grants(org=ORG)["received"]) == 1
    assert world.grants(BOB_NODE).grants(org=ORG)["limitedGrantors"] == []

    world.fill("alice", LIMIT, revoked_at_ms=world.clock[0])
    listed = world.grants(BOB_NODE).grants(org=ORG)

    assert listed["received"] == []
    assert listed["limitedGrantors"] == [{"org": ORG, "grantor": "alice", "reason": "documents"}]


def test_the_grantor_still_lists_its_own_documents_over_the_limit(world: World) -> None:
    made = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")
    world.fill("alice", LIMIT, revoked_at_ms=world.clock[0])

    listed = world.grants().grants(org=ORG)

    assert [item["grantId"] for item in listed["granted"]] == [made["grant"]["grantId"]]
    assert listed["documentCount"] == {ORG: LIMIT + 1}
