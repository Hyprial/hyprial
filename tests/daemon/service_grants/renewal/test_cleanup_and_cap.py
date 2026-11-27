"""The grantor deletes its own long-dead documents and stops issuing ids at a cap.

Decision (service grant integration fix, 2026-11-27): every grant or revoke
also deletes the grantor's own documents in that org that were revoked, or
expired, more than seven days earlier. A failed deletion never fails the
write; it is one log event. A new ``grantId`` is refused once the grantor
holds 240 documents in the org after that cleanup, with a message that says
why and what to do; rewriting an effective grant in place is never refused.
``service.grants`` reports each org's document count beside the limit the
recipient reads by (256).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.kernel import DaemonRequestError
from tests.daemon.service_grants.renewal._world import (
    BOB_NODE,
    CAROL_NODE,
    DAY_MS,
    MEMBERS,
    ORG,
    PORT,
    World,
)

CAP = 240
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


def ids(world: World) -> set[str]:
    return {doc["grantId"] for doc in world.grant_docs()}


def test_a_grant_deletes_documents_revoked_more_than_seven_days_ago(world: World) -> None:
    old = world.grants().grant(org=ORG, port=PORT, device=CAROL_NODE.slug, name="magpie")
    world.grants().revoke(org=ORG, grant_id=old["grant"]["grantId"])
    world.clock[0] += DAY_MS
    recent = world.grants().grant(org=ORG, port=PORT, device=CAROL_NODE.slug, name="magpie")
    world.grants().revoke(org=ORG, grant_id=recent["grant"]["grantId"])
    world.clock[0] += 6 * DAY_MS + 1

    made = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")

    assert ids(world) == {recent["grant"]["grantId"], made["grant"]["grantId"]}
    [cleaned] = world.events_of("service.grant.cleaned")
    assert cleaned == {"org": ORG, "grantor": "alice", "grantIds": [old["grant"]["grantId"]]}


def test_a_revoke_deletes_documents_expired_more_than_seven_days_ago(world: World) -> None:
    expired = world.grants().grant(
        org=ORG, port=PORT, device=CAROL_NODE.slug, name="magpie",
        expires_at_ms=world.clock[0] + 1,
    )
    live = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")
    world.clock[0] += 7 * DAY_MS + 2

    world.grants().revoke(org=ORG, grant_id=live["grant"]["grantId"])

    assert ids(world) == {live["grant"]["grantId"]}
    assert expired["grant"]["grantId"] not in ids(world)


def test_an_update_in_place_also_cleans_up(world: World) -> None:
    old = world.grants().grant(org=ORG, port=PORT, device=CAROL_NODE.slug, name="magpie")
    world.grants().revoke(org=ORG, grant_id=old["grant"]["grantId"])
    live = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")
    world.clock[0] += 8 * DAY_MS

    again = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")

    assert again["updated"] is True
    assert ids(world) == {live["grant"]["grantId"]}


def test_a_failed_deletion_never_fails_the_grant(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    old = world.grants().grant(org=ORG, port=PORT, device=CAROL_NODE.slug, name="magpie")
    world.grants().revoke(org=ORG, grant_id=old["grant"]["grantId"])
    world.clock[0] += 8 * DAY_MS

    def refuse(*_args: object, **_kwargs: object) -> bool:
        raise OrgFsError("io-error", {"detail": "synthetic"})

    monkeypatch.setattr(world.store, "remove_service_grant", refuse)
    made = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")

    assert made["updated"] is False
    assert ids(world) == {old["grant"]["grantId"], made["grant"]["grantId"]}
    [failed] = world.events_of("service.grant.cleanup_failed")
    assert failed == {
        "org": ORG,
        "grantor": "alice",
        "grantId": old["grant"]["grantId"],
        "errorCode": "io-error",
    }


def test_a_new_grant_id_is_refused_at_the_cap(world: World) -> None:
    world.fill("alice", CAP - 1, revoked_at_ms=world.clock[0])
    live = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")
    assert len(ids(world)) == CAP

    with pytest.raises(DaemonRequestError) as refused:
        world.grants().grant(org=ORG, port=PORT, device=CAROL_NODE.slug, name="magpie")

    assert refused.value.code == "SERVICE_UNAVAILABLE"
    assert str(CAP) in str(refused.value)
    assert "撤销不用的授权，7 天后自动清理" in str(refused.value)
    assert len(ids(world)) == CAP
    renewed = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")
    assert renewed["updated"] is True
    assert renewed["grant"]["grantId"] == live["grant"]["grantId"]


def test_the_cap_counts_after_cleanup(world: World) -> None:
    world.fill("alice", CAP, revoked_at_ms=world.clock[0])
    world.clock[0] += 7 * DAY_MS + 1

    made = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")

    assert made["updated"] is False
    assert ids(world) == {made["grant"]["grantId"]}


def test_the_listing_reports_the_document_count_and_limit(world: World) -> None:
    world.fill("alice", 3, revoked_at_ms=world.clock[0])
    world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie")

    listed = world.grants().grants(org=ORG)
    assert listed["documentCount"] == {ORG: 4}
    assert listed["documentLimit"] == LIMIT
    unfiltered = world.grants().grants()
    assert unfiltered["documentCount"] == {ORG: 4}
    assert world.grants(BOB_NODE).grants()["documentCount"] == {ORG: 0}
