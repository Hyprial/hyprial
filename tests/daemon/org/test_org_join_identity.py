"""An invite link is issued per person: another identity holding it is refused.

Two real OrgFS runtimes on one in-memory mesh; the joiner runs the full
``OrgNetworkService.execute`` workflow. An explicit membership authority double
binds v2 redemption to the device owner before OrgFS can join. The in-memory
mesh models the already-admitted private path; this is not transport identity
or cryptographic authentication acceptance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.org.network.directory import (
    InvalidDeviceQuarantine,
    OrgFsDirectoryStore,
)
from hyprial.daemon.impl.org.network.service import OrgNetworkError, OrgNetworkService
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.identity.impl.device import DeviceRecord, write_device_record
from tests.support.tailnet_membership import FakeMembershipSidecar, key
from tests.support.tailnet_daemon import bind_admitted

from ..orgfs.presence.test_orgfs_rebuild_catchup import _Bus

OWNER = "owner@example"
INVITEE = "x@example"
OTHER = "y@example"
ORG = "group-acme"
# Dynamic clock: a hardcoded past NOW made the invite's +24h expiry land
# inside the test run once wall-clock passed it (intg red 2026-10-09T12:00Z).
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _home(root: Path, owner: str, device_id: str) -> Path:
    write_device_record(
        root,
        DeviceRecord(
            device_id=device_id,
            owner=owner,
            server_public=key("server-" + device_id),
            client_public=key("client-" + device_id),
        ),
    )
    return root


def _service(runtime: OrgFsRuntime, home: Path, owner: str, node_id: str,
             membership: FakeMembershipSidecar) -> OrgNetworkService:
    return OrgNetworkService(
        fs=runtime.facade,
        home=home,
        node_id=node_id,
        owner=owner,
        invalid_devices=InvalidDeviceQuarantine(),
        map_peer=lambda _peer, _address: 41000,
        read_own_address=lambda: membership.local["address"],
        membership_request=membership.membership_request,
        join_timeout_seconds=0.0,
        sleep=lambda _seconds: None,
        now=lambda: NOW,
    )


class _Org:
    """An owner node that created ``ORG`` and invited ``INVITEE``."""

    def __init__(self, tmp_path: Path) -> None:
        self.bus = _Bus()
        self.runtime = OrgFsRuntime(
            tmp_path / "owner-state", node_id="node-owner", author=f"user:{OWNER}"
        )
        bind_admitted(
            self.runtime, self.bus.session("node-owner"), supplier_online=lambda _peer: True,
        )
        self.membership = FakeMembershipSidecar(owner=OWNER, device_id="node-owner", now_ms=None)
        self.service = _service(
            self.runtime, _home(tmp_path / "owner-home", OWNER, "node-owner"),
            OWNER, "node-owner", self.membership,
        )
        created = self.service.create(ORG)
        self.space_id = created["spaceId"]
        self.acl_space_id = created["aclSpaceId"]
        self.link = self.service.invite(ORG, INVITEE)["link"]
        for mesh in self.runtime._meshes.values():
            mesh.announce()

    def member_users(self) -> dict[str, set[str]]:
        return {
            space_id: {member.user for member in self.runtime.facade.members(space_id)}
            for space_id in (self.space_id, self.acl_space_id)
        }

    def device_owners(self) -> set[str]:
        store = OrgFsDirectoryStore(self.runtime.facade)
        return {device.owner for device in store.list_devices(ORG)}


def _joiner(tmp_path: Path, org: _Org, author: str, node_id: str) -> tuple[Any, list[str]]:
    """A second node whose daemon identity is ``author``; returns its puts."""

    runtime = OrgFsRuntime(tmp_path / f"{node_id}-state", node_id=node_id, author=f"user:{author}")
    session = org.bus.session(node_id)
    puts: list[str] = []
    original_put = session.put

    def recording_put(key: str, payload: bytes) -> None:
        puts.append(key)
        original_put(key, payload)

    session.put = recording_put  # type: ignore[method-assign]
    bind_admitted(
        runtime, session,
        supplier_online=lambda _peer: True,
        holder_candidates=lambda: ("node-owner",),
    )
    service = _service(runtime, _home(tmp_path / f"{node_id}-home", author, node_id),
                       author, node_id, org.membership.for_device(author, node_id))
    return (runtime, service), puts


def test_another_identity_holding_the_invitees_link_is_refused(tmp_path: Path) -> None:
    org = _Org(tmp_path)
    (runtime, service), puts = _joiner(tmp_path, org, OTHER, "node-y")
    try:
        before = org.member_users()

        with pytest.raises(OrgNetworkError) as refused:
            service.execute(org.link)

        assert refused.value.code == "INVITE_INVALID"
        # Refused at v2 redemption, before join-org; neither space was copied.
        held = {space.space_id for space in runtime.facade.spaces()}
        assert held.isdisjoint({org.space_id, org.acl_space_id})
        assert org.member_users() == before
        assert all(f"user:{OTHER}" not in users for users in before.values())
        assert OTHER not in org.device_owners()
        assert [key for key in puts if "/log/" in key] == []
    finally:
        runtime.close()
        org.runtime.close()


def test_the_invitee_with_the_same_link_joins(tmp_path: Path) -> None:
    """Control: the refusal above is about identity, not a broken join."""

    org = _Org(tmp_path)
    (runtime, service), _puts = _joiner(tmp_path, org, INVITEE, "node-x")
    try:
        result = service.execute(org.link)

        assert [step["kind"] for step in result["steps"]][:2] == ["connect-inviter", "join-org"]
        assert {space.space_id for space in runtime.facade.spaces()} >= {
            org.space_id,
            org.acl_space_id,
        }
    finally:
        runtime.close()
        org.runtime.close()


@pytest.mark.xfail(
    reason=(
        "known gap: the supplier trusts the self-declared requester.author. "
        "orgfs/storage/store/admission.py: 'This boundary assumes origin.author "
        "is genuine; author signatures are a separate prerequisite'; "
        "orgfs/storage/store/__init__.py: 'Authors are self-asserted until "
        "signatures land'.  Passes once identity is bound to the peer's "
        "authenticated device key (node-authentication card 21aafdb0)."
    ),
    raises=AssertionError,
    strict=False,
)
def test_another_identity_declaring_itself_the_invitee_is_refused(tmp_path: Path) -> None:
    """y's machine (device record y) runs its daemon as x and uses x's link."""

    org = _Org(tmp_path)
    runtime = OrgFsRuntime(
        tmp_path / "node-y-state", node_id="node-y", author=f"user:{INVITEE}"
    )
    bind_admitted(
        runtime, org.bus.session("node-y"),
        supplier_online=lambda _peer: True,
        holder_candidates=lambda: ("node-owner",),
    )
    service = _service(
        runtime, _home(tmp_path / "node-y-home", OTHER, "node-y"), INVITEE, "node-y",
        # Match real up.owner: device-record owner, not claimed daemon author.
        org.membership.for_device(OTHER, "node-y"),
    )
    try:
        # The unchanged xfail marker concerns unsigned OrgFS authors. This
        # earlier fixture denial does not prove that deeper gap is fixed.
        with pytest.raises(OrgNetworkError) as refused:
            service.execute(org.link)
        assert refused.value.code == "INVITE_INVALID"
        held = {space.space_id for space in runtime.facade.spaces()}
        assert held.isdisjoint({org.space_id, org.acl_space_id})
    finally:
        runtime.close()
        org.runtime.close()
