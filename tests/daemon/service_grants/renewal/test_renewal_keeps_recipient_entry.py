"""After the grantor renews, the recipient keeps its entry and gets the new Key.

Decision (service grant integration fix, 2026-11-27): a renewal rewrites the
grant in place, so the recipient's derived service entry and its Key entry
keep their names; whatever the recipient configured on the Key entry stays,
and only the Key value changes. Driven end to end over one OrgFS runtime: the
grantor's real service writes, the recipient's real consumer and
service-connect manager read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from hyprial.daemon.impl.service_grants.consumer import entry_id_for_grant
from hyprial.identity import SecretResolver, read_user_provider_value
from tests.daemon.service_grants.consumer.support import write_user_provider
from tests.daemon.service_grants.renewal._world import (
    BOB_NODE,
    DAY_MS,
    MEMBERS,
    ORG,
    PORT,
    ROTATED,
    SECRET,
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


def test_a_renewal_keeps_the_recipients_entry_and_updates_its_key(world: World, actor_host) -> None:
    first = world.grants().grant(
        org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie",
        expires_at_ms=world.clock[0] + DAY_MS,
        credential={"entryId": "gateway", "fieldName": "API_KEY"},
    )
    entry_id = entry_id_for_grant(first["grant"]["grantId"])
    bob = world.homes["bob"]
    source = world.consumer()
    source.refresh()
    manager, _controller = world.manager(source, actor_host)
    try:
        manager.handle("service.connect", {"name": "magpie"})
        assert read_user_provider_value(bob, entry_id, "KEY") == SECRET
        SecretResolver(bob, None).write_user_provider(  # type: ignore[arg-type]
            entry_id, {"KEY": SECRET, "BASE_URL": "http://127.0.0.1:9/v1"}
        )
        before = {row["name"]: row for row in manager.handle("service.list", {})["services"]}

        write_user_provider(world.homes["alice"], "gateway", {"API_KEY": ROTATED})
        world.clock[0] += DAY_MS // 2
        renewed = world.grants().grant(
            org=ORG, port=PORT, device=BOB_NODE.slug, name="magpie",
            expires_at_ms=world.clock[0] + DAY_MS,
            credential={"entryId": "gateway", "fieldName": "API_KEY"},
        )
        source.refresh()
        manager.reconcile()

        assert renewed["grant"]["grantId"] == first["grant"]["grantId"]
        assert read_user_provider_value(bob, entry_id, "KEY") == ROTATED
        assert read_user_provider_value(bob, entry_id, "BASE_URL") == "http://127.0.0.1:9/v1"
        assert sorted(path.stem for path in (bob / "secrets" / "providers").glob("*.json")) == [entry_id]
        after = {row["name"]: row for row in manager.handle("service.list", {})["services"]}
        assert list(after) == list(before) == ["magpie"]
        assert after["magpie"]["state"] == before["magpie"]["state"]
        assert world.recorder.of("service.grant.withdrawn") == []

        world.clock[0] += DAY_MS // 2 + 1
        source.refresh()
        assert read_user_provider_value(bob, entry_id, "KEY") == ROTATED
    finally:
        manager.close(2.0)
