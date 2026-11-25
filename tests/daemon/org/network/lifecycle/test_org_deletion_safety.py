"""OC7 deletion safety: nothing irreversible happens before the tailnet can close.

Covers the critic findings on the first OC7 cut: a stale preview or a
non-authority device must be refused before the OrgFS tombstone; the authority
can finish an org whose marker is deleted while its tailnet is still open;
``leave`` never forgets an open tailnet on its authority; an unconfirmed
preview never blocks ``org create``; and the closed roster stays fetchable so
members learn of the deletion (contract OC0 §2.2, OC7 row).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.forwarding.membership.hardening.left_orgs import read_left_orgs
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.org.network.service import OrgNetworkError

from tests.daemon.org.network.lifecycle.test_org_deletion import (
    BOB,
    NODE_ID,
    OWNER,
    _org_meta,
    _seed_member,
    _service,
    _space_id,
)
from tests.support.tailnet_membership import member


def _journal(home: Path) -> dict:
    return json.loads((home / "state/org/deletions/group-acme.json").read_text())


def test_stale_preview_is_refused_before_the_orgfs_tombstone(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    preview = service.delete("group-acme")
    _seed_member(runtime, "group-acme")
    table = runtime.membership_sidecar.tailnets[_space_id(runtime, "group-acme")]
    table["revision"] += 1

    with pytest.raises(OrgNetworkError) as raised:
        service.delete("group-acme", confirmed=True, operation_id=preview["operationId"])

    assert raised.value.code == "REVISION_CONFLICT"
    assert _org_meta(runtime, "group-acme")["state"] == "active"
    assert not table.get("closed")
    assert "orgfsReceipt" not in _journal(home)
    assert service.list()["orgs"][0]["org"] == "group-acme"


def test_unconfirmed_preview_rebinds_after_a_roster_change(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    first = service.delete("group-acme")
    assert service.delete("group-acme")["operationId"] == first["operationId"]
    _seed_member(runtime, "group-acme")
    table = runtime.membership_sidecar.tailnets[_space_id(runtime, "group-acme")]
    table["revision"] += 1

    rebound = service.delete("group-acme")

    assert rebound["operationId"] != first["operationId"]
    assert rebound["deviceIds"] == ["node-b"]
    assert rebound["expectedRevision"] == table["revision"]
    result = service.delete(
        "group-acme", confirmed=True, operation_id=rebound["operationId"]
    )
    assert result["phase"] == "complete"
    assert table["closed"] is True


def test_non_authority_device_is_refused_at_preview_and_confirmation(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    preview = service.delete("group-acme")
    other = _service(runtime, home, node_id="node-z")

    with pytest.raises(OrgNetworkError) as at_preview:
        other.delete("group-acme")
    assert at_preview.value.code == "AUTHORITY_REQUIRED"
    with pytest.raises(OrgNetworkError) as at_confirm:
        other.delete("group-acme", confirmed=True, operation_id=preview["operationId"])
    assert at_confirm.value.code == "AUTHORITY_REQUIRED"

    assert _org_meta(runtime, "group-acme")["state"] == "active"
    assert _journal(home)["phase"] == "pending-confirmation"


def test_authority_finishes_an_org_whose_marker_is_deleted_but_tailnet_open(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")
    service._store.put_org_meta(
        "group-acme", {"state": "deleted", "deleteOperationId": "elsewhere"}
    )
    table = runtime.membership_sidecar.tailnets[_space_id(runtime, "group-acme")]

    preview = service.delete("group-acme")
    assert preview["phase"] == "pending-confirmation"
    assert preview["deviceIds"] == ["node-b"]
    result = service.delete(
        "group-acme", confirmed=True, operation_id=preview["operationId"]
    )

    assert result["phase"] == "complete"
    assert table["closed"] is True
    assert _org_meta(runtime, "group-acme")["deleteOperationId"] == "elsewhere"


def test_conflict_after_the_tombstone_is_finished_by_a_fresh_preview(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")
    preview = service.delete("group-acme")
    runtime.membership_sidecar.failures["tailnet-shutdown"] = "REVISION_CONFLICT"

    with pytest.raises(OrgNetworkError) as raised:
        service.delete("group-acme", confirmed=True, operation_id=preview["operationId"])
    assert raised.value.code == "REVISION_CONFLICT"
    assert _org_meta(runtime, "group-acme")["state"] == "deleted"

    del runtime.membership_sidecar.failures["tailnet-shutdown"]
    rebound = service.delete("group-acme")
    assert rebound["phase"] == "pending-confirmation"
    assert rebound["orgfsMarked"] is True
    result = service.delete(
        "group-acme", confirmed=True, operation_id=rebound["operationId"]
    )
    assert result["phase"] == "complete"
    table = runtime.membership_sidecar.tailnets[_space_id(runtime, "group-acme")]
    assert table["closed"] is True


def test_leave_on_the_authority_of_an_open_tailnet_is_refused(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")
    space_id = _space_id(runtime, "group-acme")
    service._store.put_org_meta("group-acme", {"state": "deleted"})

    with pytest.raises(OrgNetworkError) as raised:
        service.leave("group-acme", confirmed=True)

    assert raised.value.code == "LAST_AUTHORITY_REFUSED"
    assert space_id in runtime.membership_sidecar.tailnets
    assert not runtime.membership_sidecar.tailnets[space_id].get("closed")
    assert read_left_orgs(home) == []


def test_sidecar_refuses_an_org_deleted_forget_of_an_open_authority_tailnet(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    space_id = _space_id(runtime, "group-acme")

    with pytest.raises(Exception) as raised:
        runtime.membership_sidecar.membership_request(
            "tailnet-forget",
            {"operationId": "f-1", "tailnetId": space_id, "orgDeleted": True},
        )

    assert getattr(raised.value, "code", None) == "LAST_AUTHORITY_REFUSED"
    assert space_id in runtime.membership_sidecar.tailnets


def test_unconfirmed_preview_never_blocks_org_create(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    preview = service.delete("group-acme")

    resumed = service.create("group-acme")

    assert resumed["org"] == "group-acme"
    assert not (home / "state/org/deletions/group-acme.json").exists()
    with pytest.raises(OrgNetworkError) as stale:
        service.delete("group-acme", confirmed=True, operation_id=preview["operationId"])
    assert stale.value.code == "REMOVAL_CONFIRMATION_STALE"
    assert _org_meta(runtime, "group-acme")["state"] == "active"


def test_committed_tombstone_still_refuses_org_create(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    preview = service.delete("group-acme")
    runtime.membership_sidecar.failures["tailnet-shutdown"] = "MEMBERSHIP_BACKUP_FAILED"
    with pytest.raises(OrgNetworkError):
        service.delete("group-acme", confirmed=True, operation_id=preview["operationId"])

    with pytest.raises(OrgNetworkError) as raised:
        service.create("group-acme")

    assert raised.value.code == "ORG_ALREADY_DELETED"
    assert _journal(home)["orgfsReceipt"] == "committed"


def test_closed_roster_stays_fetchable_by_members_after_deletion(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")
    space_id = _space_id(runtime, "group-acme")
    preview = service.delete("group-acme")

    result = service.delete(
        "group-acme", confirmed=True, operation_id=preview["operationId"]
    )

    assert result["phase"] == "complete"
    assert result["tailnetClosed"] is True
    assert result["tailnetForgotten"] is False
    bob_sidecar = runtime.membership_sidecar.for_device(BOB, "node-b")
    roster = bob_sidecar.membership_request("roster-fetch", {"tailnetId": space_id})
    assert roster["closed"] is True
    assert {row["state"] for row in roster["members"]} == {"tombstone"}


def test_member_leave_on_a_closed_tailnet_is_local_cleanup_without_the_marker(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")
    space_id = _space_id(runtime, "group-acme")
    preview = service.delete("group-acme")
    service.delete("group-acme", confirmed=True, operation_id=preview["operationId"])
    service._store.put_org_meta("group-acme", {"state": "active"})
    runtime.facade.author = f"user:{BOB}"
    bob = _service(runtime, home, owner=BOB, node_id="node-b")

    result = bob.leave("group-acme", confirmed=True)

    assert result["phase"] == "complete"
    assert result["reason"] == "org-deleted"
    assert read_left_orgs(home) == ["group-acme"]
    assert space_id not in runtime.membership_sidecar.tailnets


def test_owner_leave_after_a_completed_deletion_forgets_the_closed_tailnet(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    space_id = _space_id(runtime, "group-acme")
    preview = service.delete("group-acme")
    service.delete("group-acme", confirmed=True, operation_id=preview["operationId"])
    assert runtime.membership_sidecar.tailnets[space_id]["closed"] is True

    result = service.leave("group-acme", confirmed=True)

    assert result["reason"] == "org-deleted"
    assert space_id not in runtime.membership_sidecar.tailnets


def test_member_view_seeded_open_still_departs_on_the_marker(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")
    space_id = _space_id(runtime, "group-acme")
    preview = service.delete("group-acme")
    service.delete("group-acme", confirmed=True, operation_id=preview["operationId"])
    bob_sidecar = runtime.membership_sidecar.for_device(BOB, "node-b")
    runtime.membership_sidecar.seed(space_id, members=[member(OWNER, NODE_ID), member(BOB, "node-b")])
    runtime.facade.author = f"user:{BOB}"
    bob = _service(runtime, home, owner=BOB, node_id="node-b")

    result = bob.leave("group-acme", confirmed=True)

    assert result["reason"] == "org-deleted"
    assert space_id not in bob_sidecar.tailnets


def test_unavailable_sidecar_fails_closed_before_the_orgfs_tombstone(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")
    table = runtime.membership_sidecar.tailnets[_space_id(runtime, "group-acme")]
    down = _service(runtime, home, membership_request=None)

    with pytest.raises(OrgNetworkError) as raised:
        down.delete("group-acme")

    assert raised.value.code == "MEMBERSHIP_NOT_INITIALIZED"
    assert _org_meta(runtime, "group-acme")["state"] == "active"
    assert not (home / "state/org/deletions/group-acme.json").exists()
    assert not table.get("closed")
    service._store.put_org_meta("group-acme", {"state": "deleted"})
    with pytest.raises(OrgNetworkError) as refused:
        down.leave("group-acme")
    assert refused.value.code == "MEMBERSHIP_NOT_INITIALIZED"
    assert "group-acme" not in read_left_orgs(home)


def test_partially_deleted_org_with_an_open_tailnet_is_finished(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    _seed_member(runtime, "group-acme")
    space_id = _space_id(runtime, "group-acme")
    table = runtime.membership_sidecar.tailnets[space_id]
    service._store.put_org_meta("group-acme", {"state": "deleted"})
    journal = home / "state/org/deletions/group-acme.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                "operationId": "earlier-delete",
                "org": "group-acme",
                "spaceId": space_id,
                "deviceIds": [],
                "expectedRevision": None,
                "authorityDeviceId": None,
                "phase": "complete",
                "confirmed": True,
                "orgfsReceipt": "committed",
                "tailnetShutdown": None,
            }
        )
    )

    preview = service.delete("group-acme")

    assert preview["phase"] == "pending-confirmation"
    assert preview["orgfsMarked"] is True
    assert preview["expectedRevision"] == table["revision"]
    done = service.delete(
        "group-acme", confirmed=True, operation_id=preview["operationId"]
    )
    assert done["phase"] == "complete"
    assert table["closed"] is True
    assert service.delete("group-acme")["operationId"] == preview["operationId"]
    assert service.leave("group-acme")["reason"] == "org-deleted"


def test_authority_is_the_sidecar_role_not_a_node_id_match(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    table = runtime.membership_sidecar.tailnets[_space_id(runtime, "group-acme")]
    stranger = runtime.membership_sidecar.for_device(OWNER, "node-z")
    lookalike = _service(
        runtime, home, membership_request=stranger.membership_request
    )

    with pytest.raises(OrgNetworkError) as raised:
        lookalike.delete("group-acme")
    assert raised.value.code == "AUTHORITY_REQUIRED"
    assert _org_meta(runtime, "group-acme")["state"] == "active"
    with pytest.raises(OrgNetworkError) as gated:
        lookalike.require_roster_write_authority(
            "group-acme", _space_id(runtime, "group-acme")
        )
    assert gated.value.code == "ORG_ACTION_NOT_AUTHORIZED"

    renamed = _service(
        runtime,
        home,
        membership_request=runtime.membership_sidecar.membership_request,
    )
    renamed._node_id = "node-renamed"
    service._store.put_org_meta("group-acme", {"state": "deleted"})
    with pytest.raises(OrgNetworkError) as kept:
        renamed.leave("group-acme")
    assert kept.value.code == "LAST_AUTHORITY_REFUSED"
    assert not table.get("closed")
    assert "group-acme" not in read_left_orgs(home)


def test_network_view_reports_a_deleted_org_until_it_is_left(
    runtime: OrgFsRuntime, home: Path
) -> None:
    service = _service(runtime, home)
    service.create("group-acme")
    table = runtime.membership_sidecar.tailnets[_space_id(runtime, "group-acme")]
    assert not service.network()["dataProblems"]

    table["closed"] = True
    closed = [
        problem
        for problem in service.network()["dataProblems"]
        if problem["reason"] == "org-deleted"
    ]
    assert closed == [
        {
            "org": "group-acme",
            "path": "tailnet",
            "reason": "org-deleted",
            "value": "hyprial org leave group-acme",
        }
    ]

    service._store.put_org_meta("group-acme", {"state": "deleted"})
    marked = service.network()["dataProblems"]
    assert [problem["path"] for problem in marked] == ["directory/org.json"]
    service.leave("group-acme")
    assert not service.network()["dataProblems"]
