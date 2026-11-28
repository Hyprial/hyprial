"""A ``@members`` grant admits the members of its own org, and nobody else.

Decision (service grant integration fix, 2026-11-27): a ``@members`` grant is
expanded to keys, never to ``allow``. The keys are those of every active
device in the roster of the org's effective tailnet. A port a device exposes
is reachable over every tailnet that device is on, so ``allow`` admitted the
members of all of them. A sub-org inherits its parent's tailnet, whose roster
lists the parent's members too, so for a sub-org the roster is also
intersected with the sub-org's own directory: a device counts only when its
owner is a member of the sub-org and has published that device there. A
sub-org directory that cannot be read tightens, as an unreadable roster does.

Grants, device rows and memberships are written by product code over a real
OrgFS runtime. The roster is the one test input, because in production it
comes from the sidecar; the supervisor is a recorder of what would be pushed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from hyprial.daemon.impl.service_grants.enforcement import ServiceAdmissionEnforcer
from hyprial.identity import ORG_META_DOC, DirectoryDevice
from tests.daemon.service_grants.enforcement._support import (
    ALICE_NODE,
    BOB_NODE,
    CAROL_NODE,
    MEMBERS,
    ORG,
    PORT,
    Device,
    Harness,
    peer_key,
)

BETA = "group-beta"
LAB = "group-acme-lab"
DAVE_NODE = Device("dave", "device-d")
KEY_UNAVAILABLE = "service.grant.key_unavailable"
DIRECTORY_UNAVAILABLE = "service.grant.directory_unavailable"


class _Supervisor:
    """Records each push; its controller carries the exposing device's server key."""

    def __init__(self) -> None:
        self.backend = SimpleNamespace(controller=SimpleNamespace(server_public=ALICE_NODE.server_public))
        self.pushes: list[dict[str, Any]] = []

    def endpoints(self) -> Any:
        return self.backend

    def expose(self, exposure: Any, *, admission: str, keys: list[str]) -> None:
        self.pushes.append({"port": exposure["port"], "admission": admission, "keys": sorted(keys)})


def _row(member: Device) -> dict[str, Any]:
    return {
        "owner": member.owner,
        "deviceId": member.device_id,
        "masterPublic": member.master_public,
        "serverPublic": member.server_public,
        "address": member.address,
        "state": "active",
        "revision": 1,
        "clientPublicByServer": {ALICE_NODE.server_public: peer_key(member, ALICE_NODE)},
    }


def _tailnet(tailnet_id: str, *members: Device) -> dict[str, Any]:
    return {
        "tailnetId": tailnet_id,
        "role": "member",
        "revision": 1,
        "authorityDeviceId": ALICE_NODE.device_id,
        "memberReady": True,
        "leaseRemainingSeconds": None,
        "activeCount": len(members),
        "tombstoneCount": 0,
        "code": None,
        "members": [_row(member) for member in members],
    }


def _device(member: Device) -> DirectoryDevice:
    return DirectoryDevice(
        device_id=member.device_id,
        owner=member.owner,
        server_public=member.server_public,
        client_public=member.master_public,
        address=member.address,
        key_generation=1,
        updated_at="2026-11-26T00:00:00Z",
    )


class _World(Harness):
    def space(self, org: str, *members: Device, parent: str | None = None) -> str:
        space = self.fs.create_space(org)
        for member in members:
            if member.owner != ALICE_NODE.owner:
                self.fs.invite(space.space_id, f"user:{member.owner}")
        self.store.ensure_org_directory_roots(org)
        if parent is not None:
            self.fs.write_text(space.space_id, ORG_META_DOC, json.dumps({"parent": parent}))
        for member in members:
            with self.as_(member.owner):
                self.store.put_device(org, _device(member))
        return space.space_id

    def enforcer(self, roster: dict[str, Any], supervisor: _Supervisor, **overrides: Any) -> ServiceAdmissionEnforcer:
        options: dict[str, Any] = {
            "supervisor": supervisor,
            "exposure_store": self.exposures,
            "store": self.store,
            "fs": self.fs,
            "home": self.homes[ALICE_NODE.owner],
            "owner": ALICE_NODE.owner,
            "device_id": ALICE_NODE.device_id,
            "grants_service": self.grants,
            "roster_reader": lambda: roster,
            "now_ms": lambda: self.clock[0],
            "logger": self.log,
            "interval_seconds": 0.0,
        }
        options.update(overrides)
        return ServiceAdmissionEnforcer(**options)

    def logged(self, event: str) -> list[dict[str, Any]]:
        return [fields for _level, _component, name, fields in self.events if name == event]


@pytest.fixture
def world(tmp_path: Path) -> Iterator[_World]:
    made = _World(tmp_path)
    try:
        for member in MEMBERS:
            made.publish(member)
        yield made
    finally:
        made.close()


def test_a_members_grant_does_not_admit_a_device_that_is_only_in_another_org_of_the_grantor(
    world: _World,
) -> None:
    beta_tailnet = world.space(BETA, ALICE_NODE, DAVE_NODE)
    roster = {
        "readiness": {},
        "tailnets": [
            _tailnet(world.space_id, ALICE_NODE, BOB_NODE, CAROL_NODE),
            _tailnet(beta_tailnet, ALICE_NODE, DAVE_NODE),
        ],
    }
    world.grants().grant(org=ORG, port=PORT, device="@members")
    supervisor = _Supervisor()

    world.enforcer(roster, supervisor).apply_now()

    [push] = supervisor.pushes
    assert push["admission"] == "keys"
    assert peer_key(DAVE_NODE, ALICE_NODE) not in push["keys"]
    assert set(push["keys"]) >= {peer_key(BOB_NODE, ALICE_NODE), peer_key(CAROL_NODE, ALICE_NODE)}


def test_a_sub_orgs_members_grant_does_not_admit_a_device_that_is_only_in_the_parent(
    world: _World,
) -> None:
    world.space(LAB, ALICE_NODE, BOB_NODE, parent=ORG)
    roster = {
        "readiness": {},
        "tailnets": [_tailnet(world.space_id, ALICE_NODE, BOB_NODE, CAROL_NODE)],
    }
    world.grants().grant(org=LAB, port=PORT, device="@members")
    supervisor = _Supervisor()

    world.enforcer(roster, supervisor).apply_now()

    [push] = supervisor.pushes
    assert push["admission"] == "keys"
    assert peer_key(BOB_NODE, ALICE_NODE) in push["keys"]
    assert peer_key(CAROL_NODE, ALICE_NODE) not in push["keys"]


def test_an_unreadable_sub_org_directory_tightens_and_is_reported(world: _World) -> None:
    world.space(LAB, ALICE_NODE, BOB_NODE, parent=ORG)
    roster = {
        "readiness": {},
        "tailnets": [_tailnet(world.space_id, ALICE_NODE, BOB_NODE, CAROL_NODE)],
    }
    world.grants().grant(org=LAB, port=PORT, device="@members")
    supervisor = _Supervisor()
    original = world.store.list_devices

    def failing(org: str) -> Any:
        if org == LAB:
            raise OSError("directory unavailable")
        return original(org)

    world.store.list_devices = failing  # type: ignore[method-assign]

    world.enforcer(roster, supervisor).apply_now()

    [push] = supervisor.pushes
    assert push == {"port": PORT, "admission": "keys", "keys": []}
    assert {"org": LAB, "step": "devices"} in world.logged(DIRECTORY_UNAVAILABLE)
    assert [fields["reason"] for fields in world.logged(KEY_UNAVAILABLE)] == ["org-devices-unavailable"]


def test_an_owner_who_left_the_sub_org_is_not_admitted_through_a_stale_device_row(world: _World) -> None:
    lab = world.space(LAB, ALICE_NODE, BOB_NODE, parent=ORG)
    roster = {
        "readiness": {},
        "tailnets": [_tailnet(world.space_id, ALICE_NODE, BOB_NODE, CAROL_NODE)],
    }
    world.grants().grant(org=LAB, port=PORT, device="@members")
    world.fs.remove_member(lab, f"user:{BOB_NODE.owner}")
    supervisor = _Supervisor()

    world.enforcer(roster, supervisor).apply_now()

    [push] = supervisor.pushes
    assert push["admission"] == "keys"
    assert peer_key(BOB_NODE, ALICE_NODE) not in push["keys"]


def test_a_members_grant_never_pushes_allow(world: _World) -> None:
    roster = {"readiness": {}, "tailnets": [_tailnet(world.space_id, ALICE_NODE, BOB_NODE, CAROL_NODE)]}
    world.grants().grant(org=ORG, port=PORT, device="@members")
    supervisor = _Supervisor()

    world.enforcer(roster, supervisor).apply_now()

    assert [push["admission"] for push in supervisor.pushes] == ["keys"]
    assert sorted(supervisor.pushes[0]["keys"]) == sorted(
        [peer_key(BOB_NODE, ALICE_NODE), peer_key(CAROL_NODE, ALICE_NODE), peer_key(ALICE_NODE, ALICE_NODE)]
    )
