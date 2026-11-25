"""OC7 D5: legacy orgs without a bound org cell never get silent backfill.

Member operations (invite, leave, remove, process_leave_requests) on an org
whose directory space exists but which has no org cell must fail with the
named ``ORG_CELL_UNBOUND`` error pointing at the migration guide, and must
not create an org cell as a side effect (contract OC0 §3.9).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.org.network.directory import (
    InvalidDeviceQuarantine,
    OrgFsDirectoryStore,
)
from hyprial.daemon.impl.org.network.service import OrgNetworkError, OrgNetworkService
from hyprial.daemon.impl.forwarding.membership.hardening.left_orgs import read_left_orgs
from hyprial.identity.impl.device import DeviceRecord, write_device_record
from hyprial.identity.impl.org_directory import ORG_META_DOC, DirectoryDevice

from tests.support.tailnet_membership import FakeMembershipSidecar, key, member

OWNER = "owner@example"
BOB = "bob@example"
NODE_ID = "node-a"
OWN_ADDRESS = "tcp://10.0.0.2:7447#own-psk"
MIGRATION_DOC = "docs/guides/migration-oc7-org-lifecycle.md"


@pytest.fixture
def runtime(tmp_path: Path) -> OrgFsRuntime:
    runtime = OrgFsRuntime(tmp_path / "state", node_id=NODE_ID, author=f"user:{OWNER}")
    yield runtime
    runtime.close()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    write_device_record(
        tmp_path,
        DeviceRecord(
            device_id=NODE_ID,
            owner=OWNER,
            server_public=key("server-node-a"),
            client_public=key("client-node-a"),
        ),
    )
    return tmp_path


def _space_id(runtime: OrgFsRuntime, org: str) -> str:
    return next(space.space_id for space in runtime.facade.spaces() if space.name == org)


class _BindingProbe:
    """Read-only org-cell binding callback with recorded lookups."""

    def __init__(self, bound: bool) -> None:
        self.bound = bound
        self.lookups: list[tuple[str, str]] = []

    def __call__(self, org: str, space_id: str) -> bool:
        self.lookups.append((org, space_id))
        return self.bound


def _service(
    runtime: OrgFsRuntime,
    home: Path,
    *,
    bound: bool | None = False,
    owner: str = OWNER,
    node_id: str = NODE_ID,
) -> tuple[OrgNetworkService, _BindingProbe | None]:
    if not hasattr(runtime, "membership_sidecar"):
        runtime.membership_sidecar = FakeMembershipSidecar(now_ms=1791028800000)
    sidecar = runtime.membership_sidecar
    if (owner, node_id) != (OWNER, NODE_ID):
        sidecar = sidecar.for_device(owner, node_id)
    probe = _BindingProbe(bound) if bound is not None else None
    service = OrgNetworkService(
        fs=runtime.facade,
        home=home,
        node_id=node_id,
        owner=owner,
        membership_request=sidecar.membership_request,
        invalid_devices=InvalidDeviceQuarantine(),
        read_own_address=lambda: OWN_ADDRESS,
        now=lambda: datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC),
        org_cell_bound=probe,
    )
    return service, probe


def _seed_member(runtime: OrgFsRuntime, org: str) -> None:
    runtime.facade.invite(_space_id(runtime, org), f"user:{BOB}", "rw")
    store = OrgFsDirectoryStore(runtime.facade)
    runtime.facade.author = f"user:{BOB}"
    store.put_device(
        org,
        DirectoryDevice(
            device_id="node-b",
            owner=BOB,
            server_public=key("server-node-b"),
            client_public=key("client-node-b"),
            address="tcp://10.0.0.9:7447#node-b",
            key_generation=1,
            updated_at="2026-10-03T11:00:00+00:00",
        ),
    )
    runtime.facade.author = f"user:{OWNER}"
    table = runtime.membership_sidecar.tailnets[_space_id(runtime, org)]
    table["members"]["node-b"] = member(BOB, "node-b")


def _assert_no_backfill(runtime: OrgFsRuntime, org: str) -> None:
    space_id = _space_id(runtime, org)
    try:
        rows = runtime.facade.listdir(space_id, "cells/org")
    except Exception:
        rows = ()
    assert rows == ()
    text, _version = runtime.facade.read_text(space_id, ORG_META_DOC)
    assert json.loads(text)["state"] == "active"


def test_invite_on_an_unbound_org_fails_with_the_named_error(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service, probe = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")

    with pytest.raises(OrgNetworkError) as raised:
        service.invite("group-acme", f"user:{BOB}")

    assert raised.value.code == "ORG_CELL_UNBOUND"
    assert "'group-acme'" in str(raised.value)
    assert MIGRATION_DOC in str(raised.value)
    assert raised.value.details == {}
    assert probe is not None and probe.lookups
    _assert_no_backfill(runtime, "group-acme")


def test_remove_on_an_unbound_org_fails_with_the_named_error(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service, _probe = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")

    with pytest.raises(OrgNetworkError) as raised:
        service.remove("group-acme", f"user:{BOB}")

    assert raised.value.code == "ORG_CELL_UNBOUND"
    _assert_no_backfill(runtime, "group-acme")


def test_leave_on_an_unbound_org_fails_with_the_named_error(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service, _probe = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")
    runtime.facade.author = f"user:{BOB}"
    bob, _probe_bob = _service(runtime, home, owner=BOB, node_id="node-b")

    with pytest.raises(OrgNetworkError) as raised:
        bob.leave("group-acme", confirmed=True)

    assert raised.value.code == "ORG_CELL_UNBOUND"
    assert MIGRATION_DOC in str(raised.value)
    assert read_left_orgs(home) == []
    _assert_no_backfill(runtime, "group-acme")


def test_process_leave_requests_reports_unbound_orgs(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service, _probe = _service(runtime, home)
    service.create("group-acme")
    service.create("group-zeta")
    _seed_member(runtime, "group-acme")
    runtime.facade.author = f"user:{BOB}"
    OrgFsDirectoryStore(runtime.facade).put_leave_request(
        "group-acme",
        BOB,
        {
            "org": "group-acme",
            "user": BOB,
            "deviceId": "node-b",
            "requestedAt": "2026-10-03T12:00:00+00:00",
        },
    )
    runtime.facade.author = f"user:{OWNER}"

    result = service.process_leave_requests()

    assert result["processed"] == []
    assert result["unbound"] == ["group-acme", "group-zeta"]
    _assert_no_backfill(runtime, "group-acme")


def test_bound_orgs_keep_working(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service, _probe = _service(runtime, home, bound=True)
    service.create("group-acme")

    invited = service.invite("group-acme", f"user:{BOB}")

    assert invited["org"] == "group-acme"


def test_leave_on_a_deleted_unbound_org_still_cleans_up_locally(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service, _probe = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")
    space_id = _space_id(runtime, "group-acme")
    preview = service.delete("group-acme")
    service.delete("group-acme", confirmed=True, operation_id=preview["operationId"])

    bob_sidecar = runtime.membership_sidecar.for_device(BOB, "node-b")
    runtime.membership_sidecar.seed(space_id, members=[member(OWNER, NODE_ID), member(BOB, "node-b")])
    bob_sidecar.tailnets[space_id]["members"]["node-b"]["state"] = "tombstone"
    runtime.facade.author = f"user:{BOB}"
    bob, _probe_bob = _service(runtime, home, owner=BOB, node_id="node-b")

    result = bob.leave("group-acme", confirmed=True)

    assert result["phase"] == "complete"
    assert read_left_orgs(home) == ["group-acme"]
