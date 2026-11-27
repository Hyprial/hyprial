"""A listing over every org names the orgs it could not read.

Decision (service grant integration fix, 2026-11-27): ``service.grants``
without ``org`` keeps answering when one org's grant directory cannot be
read, but no longer reads like "nobody was granted there": the result names
each such org in ``unreadableOrgs``, and the CLI says that its grants are
unknown, not absent. Asked for that one org, the request still refuses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.kernel import DaemonRequestError
from hyprial.shell.impl.cli.commands.service import commands
from hyprial.shell.impl.cli.commands.service import grants as cli_grants
from tests.daemon.service_grants.renewal._world import BOB_NODE, MEMBERS, ORG, PORT, World
from tests.daemon.service_grants.test_ipc_and_cli import _FakeCliServices

BROKEN = "group-broken"


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    made = World(tmp_path)
    for member in MEMBERS:
        made.publish(member)
    try:
        yield made
    finally:
        made.close()


def _break(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    listed = world.store.orgs
    ids = world.store.service_grant_ids

    def orgs() -> list[str]:
        return [*listed(), BROKEN]

    def grant_ids(org: str, owner: str) -> list[str]:
        if org == BROKEN:
            raise OrgFsError("unknown-space", {"org": org})
        return ids(org, owner)

    monkeypatch.setattr(world.store, "orgs", orgs)
    monkeypatch.setattr(world.store, "service_grant_ids", grant_ids)


def test_an_unreadable_org_is_named_and_the_others_are_still_listed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = world.grants().grant(org=ORG, port=PORT, device=BOB_NODE.slug)
    _break(world, monkeypatch)

    listed = world.grants().grants()

    assert [item["grantId"] for item in listed["granted"]] == [made["grant"]["grantId"]]
    assert listed["unreadableOrgs"] == [BROKEN]
    with pytest.raises(DaemonRequestError):
        world.grants().grants(org=BROKEN)


def test_a_readable_listing_names_no_org(world: World) -> None:
    assert world.grants().grants()["unreadableOrgs"] == []


def test_the_cli_says_an_unreadable_orgs_grants_are_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    services = _FakeCliServices(
        {
            "granted": [], "received": [], "documentCount": {}, "documentLimit": 256,
            "limitedGrantors": [], "unreadableOrgs": [BROKEN],
        }
    )
    monkeypatch.setattr(commands, "get_services", lambda: services)
    monkeypatch.setattr(cli_grants, "get_services", lambda: services)

    result = CliRunner().invoke(commands.service_app, ["grants"])

    assert result.exit_code == 0, result.stderr
    assert f"Not read: {BROKEN} could not be read; its grants are unknown, not absent." in result.stdout
