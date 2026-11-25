"""OC7 org deletion lifecycle: confirmed tailnet shutdown, tombstone, cleanup.

These tests run against a real local OrgFS runtime and the in-memory v6
membership fake, covering the D1/W9 delete flow (preview -> confirmed execute
with a durable journal), the member-side deleted-marker cleanup, and the
anti-resurrection guarantees (contract OC0 §2.2, §8.1).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.forwarding.membership.removal import RemovalJournal
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.org.network.directory import (
    InvalidDeviceQuarantine,
    OrgFsDirectoryStore,
)
from hyprial.daemon.impl.org.network.service import OrgNetworkError, OrgNetworkService
from hyprial.daemon.impl.org.network.state import begin_org_create
from hyprial.daemon.impl.forwarding.membership.hardening.left_orgs import read_left_orgs
from hyprial.identity.impl.device import DeviceRecord, write_device_record
from hyprial.identity.impl.org_directory import ORG_META_DOC, DirectoryDevice

from tests.support.tailnet_membership import FakeMembershipSidecar, key, member

OWNER = "owner@example"
BOB = "bob@example"
NODE_ID = "node-a"
OWN_ADDRESS = "tcp://10.0.0.2:7447#own-psk"
NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)


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


def _service(
    runtime: OrgFsRuntime,
    home: Path,
    *,
    owner: str = OWNER,
    node_id: str = NODE_ID,
    **overrides: Any,
) -> OrgNetworkService:
    if not hasattr(runtime, "membership_sidecar"):
        runtime.membership_sidecar = FakeMembershipSidecar(now_ms=None)
    sidecar = runtime.membership_sidecar
    if (owner, node_id) != (OWNER, NODE_ID):
        sidecar = sidecar.for_device(owner, node_id)
    defaults: dict[str, Any] = {
        "membership_request": sidecar.membership_request,
        "invalid_devices": InvalidDeviceQuarantine(),
        "read_own_address": lambda: OWN_ADDRESS,
        "now": lambda: NOW,
    }
    defaults.update(overrides)
    return OrgNetworkService(
        fs=runtime.facade,
        home=home,
        node_id=node_id,
        owner=owner,
        **defaults,
    )


def _org_meta(runtime: OrgFsRuntime, org: str) -> dict[str, Any]:
    text, _version = runtime.facade.read_text(_space_id(runtime, org), ORG_META_DOC)
    return json.loads(text)


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
    table = runtime.membership_sidecar.tailnets[_space_id(runtime, org)]
    table["members"]["node-b"] = member(BOB, "node-b")
    store.put_leave_request(
        org,
        BOB,
        {"org": org, "user": BOB, "deviceId": "node-b", "requestedAt": NOW.isoformat()},
    )
    runtime.facade.author = f"user:{OWNER}"


def _confirm_delete(service: OrgNetworkService, org: str) -> dict[str, Any]:
    preview = service.delete(org)
    assert preview["phase"] == "pending-confirmation"
    return service.delete(
        org, confirmed=True, operation_id=preview["operationId"]
    )


def test_delete_preview_binds_operation_and_device_set(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")

    preview = service.delete("group-acme")

    assert preview["phase"] == "pending-confirmation"
    assert preview["org"] == "group-acme"
    assert preview["spaceId"] == _space_id(runtime, "group-acme")
    assert preview["deviceIds"] == ["node-b"]
    assert preview["operationId"]
    assert preview["irreversible"] is True
    assert _org_meta(runtime, "group-acme")["state"] == "active"
    journal = json.loads(
        (home / "state/org/deletions/group-acme.json").read_text()
    )
    assert journal["phase"] == "pending-confirmation"
    assert journal["operationId"] == preview["operationId"]


def test_delete_confirmed_marks_tombstone_shuts_down_and_keeps_closed_roster(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")
    space_id = _space_id(runtime, "group-acme")

    result = _confirm_delete(service, "group-acme")

    assert result["phase"] == "complete"
    assert result["orgfsMarked"] is True
    assert result["tailnetClosed"] is True
    assert result["tailnetForgotten"] is False
    meta = _org_meta(runtime, "group-acme")
    assert meta["state"] == "deleted"
    assert meta["deleteOperationId"] == result["operationId"]
    assert runtime.membership_sidecar.tailnets[space_id]["closed"] is True
    assert service.process_leave_requests() == {
        "processed": [], "skipped": [], "unbound": []
    }
    record = json.loads(
        (home / "state/org/deleted-orgs/group-acme.json").read_text()
    )
    assert record["spaceId"] == space_id
    assert record["operationId"] == result["operationId"]
    journal = json.loads((home / "state/org/deletions/group-acme.json").read_text())
    assert journal["phase"] == "complete"
    assert service.list()["orgs"] == []


def test_delete_confirmation_requires_the_exact_operation(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    service.delete("group-acme")
    with pytest.raises(OrgNetworkError) as raised:
        service.delete("group-acme", confirmed=True, operation_id="not-the-id")
    assert raised.value.code == "REMOVAL_CONFIRMATION_STALE"
    with pytest.raises(OrgNetworkError) as missing:
        service.delete("group-acme", confirmed=True)
    assert missing.value.code == "REMOVAL_CONFIRMATION_STALE"
    assert _org_meta(runtime, "group-acme")["state"] == "active"


def test_delete_resumes_after_a_tailnet_failure(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")
    runtime.membership_sidecar.failures["tailnet-shutdown"] = "MEMBERSHIP_BACKUP_FAILED"
    preview = service.delete("group-acme")

    with pytest.raises(OrgNetworkError) as raised:
        service.delete("group-acme", confirmed=True, operation_id=preview["operationId"])
    assert raised.value.code == "MEMBERSHIP_BACKUP_FAILED"
    assert _org_meta(runtime, "group-acme")["state"] == "deleted"
    journal = json.loads((home / "state/org/deletions/group-acme.json").read_text())
    assert journal["phase"] == "tailnet-pending"

    del runtime.membership_sidecar.failures["tailnet-shutdown"]
    result = service.delete(
        "group-acme", confirmed=True, operation_id=preview["operationId"]
    )
    assert result["phase"] == "complete"
    assert runtime.membership_sidecar.tailnets[_space_id(runtime, "group-acme")]["closed"] is True


def test_delete_revision_conflict_fails_closed_and_rebinds(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")
    preview = service.delete("group-acme")
    table = runtime.membership_sidecar.tailnets[_space_id(runtime, "group-acme")]
    table["revision"] += 1

    with pytest.raises(OrgNetworkError) as raised:
        service.delete("group-acme", confirmed=True, operation_id=preview["operationId"])
    assert raised.value.code == "REVISION_CONFLICT"
    journal = json.loads((home / "state/org/deletions/group-acme.json").read_text())
    assert journal["phase"] == "revision-conflict"
    assert not table.get("closed")

    rebind = service.delete("group-acme")
    assert rebind["phase"] == "pending-confirmation"
    assert rebind["expectedRevision"] == table["revision"]
    result = service.delete("group-acme", confirmed=True, operation_id=rebind["operationId"])
    assert result["phase"] == "complete"
    assert table["closed"] is True


def test_delete_without_a_tailnet_marks_the_orgfs_tombstone(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home, network_isolated=True)
    service.create("group-acme")

    result = _confirm_delete(service, "group-acme")

    assert result["phase"] == "complete"
    assert result["tailnetPhase"] == "absent"
    assert result["tailnetClosed"] is False
    assert _org_meta(runtime, "group-acme")["state"] == "deleted"


def test_delete_clears_residual_create_intents(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    begin_org_create(
        home, org="group-acme", node_id=NODE_ID, owner=OWNER, started_at=NOW.isoformat()
    )

    result = _confirm_delete(service, "group-acme")

    assert result["phase"] == "complete"
    intents = json.loads((home / "state/org/create-intents.json").read_text())
    assert "group-acme" not in intents


def test_delete_prevents_resurrection_of_the_same_name(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    _confirm_delete(service, "group-acme")

    with pytest.raises(OrgNetworkError) as raised:
        service.create("group-acme")
    assert raised.value.code == "ORG_ALREADY_DELETED"

    begin_org_create(
        home, org="group-acme", node_id=NODE_ID, owner=OWNER, started_at=NOW.isoformat()
    )
    with pytest.raises(OrgNetworkError) as intent:
        service.create("group-acme")
    assert intent.value.code == "ORG_ALREADY_DELETED"


def test_delete_cleans_pending_removal_journals(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")
    space_id = _space_id(runtime, "group-acme")
    journal = RemovalJournal(
        home, runtime.membership_sidecar.membership_request, local_device=NODE_ID
    )
    pending = journal.remove(space_id, BOB, confirmed=False, upper=lambda: None)
    assert pending["phase"] in ("pending-confirmation", "pending-authority")
    assert list((home / "tailnet/removals").glob("*.json"))

    result = _confirm_delete(service, "group-acme")

    assert result["phase"] == "complete"
    assert list((home / "tailnet/removals").glob("*.json")) == []


def test_member_leave_on_a_deleted_org_is_local_cleanup(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")
    space_id = _space_id(runtime, "group-acme")
    _confirm_delete(service, "group-acme")

    bob_sidecar = runtime.membership_sidecar.for_device(BOB, "node-b")
    runtime.membership_sidecar.seed(space_id, members=[member(OWNER, NODE_ID), member(BOB, "node-b")])
    bob_sidecar.tailnets[space_id]["members"]["node-b"]["state"] = "tombstone"
    runtime.facade.author = f"user:{BOB}"
    bob = _service(runtime, home, owner=BOB, node_id="node-b")

    result = bob.leave("group-acme", confirmed=True)

    assert result["phase"] == "complete"
    assert result["reason"] == "org-deleted"
    assert read_left_orgs(home) == ["group-acme"]
    assert space_id not in bob_sidecar.tailnets
    store = OrgFsDirectoryStore(runtime.facade)
    assert set(store.list_leave_requests("group-acme")) == {BOB}
    assert bob.list()["orgs"] == []


def test_invite_and_remove_on_a_deleted_org_are_refused(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")
    _confirm_delete(service, "group-acme")

    with pytest.raises(OrgNetworkError) as invited:
        service.invite("group-acme", f"user:{BOB}")
    assert invited.value.code == "ORG_ALREADY_DELETED"
    with pytest.raises(OrgNetworkError) as removed:
        service.remove("group-acme", f"user:{BOB}", confirmed=True, operation_id="x", device_ids=["node-b"])
    assert removed.value.code == "ORG_ALREADY_DELETED"
