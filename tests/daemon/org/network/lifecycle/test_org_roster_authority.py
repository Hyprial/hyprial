"""OC7 M2: org-cell roster writes follow the tailnet authority device.

The tailnet authority is pinned in settings and moved atomically by the
fenced offline ``authority-repin``; roster writes on any other device fail
with ``ORG_ACTION_NOT_AUTHORIZED`` so the new authority takes over and the
old one is revoked (contract OC0 §3.1–§3.3 M2). Also covers the ipc-level
D5 path: an org whose cell is gone is reported unbound, refuses member
operations, and is still safely deletable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.composition.cells import cell_runtime
from hyprial.daemon.impl.org.cell_actor import OrgMemberWrite, org_cell_actors
from hyprial.daemon.impl.org.network.ipc import (
    _build_service,
    ipc_org_create,
    ipc_org_delete,
    ipc_org_invite,
    ipc_org_network,
    ipc_org_remove,
)
from hyprial.daemon.impl.org.network.service import OrgNetworkError
from hyprial.kernel import DaemonRequestError, keyed_cell_id

from tests.daemon.org.cell_actor.test_org_cell_actor import (
    ALICE_DEVICE_UID,
    ALICE_OWNER,
    ALICE_UID,
    BOB_DEVICE_UID,
    BOB_OWNER,
    BOB_UID,
    _OrgApp,
)


def _bob_write() -> OrgMemberWrite:
    return OrgMemberWrite(
        uid_ref=f"u.{BOB_UID}",
        username=BOB_OWNER,
        devices=(
            {
                "deviceUid": f"d.{BOB_DEVICE_UID}",
                "keyId": None,
                "publicKey": None,
                "verifiedAtMs": 1,
            },
        ),
    )


def _repin(app: _OrgApp, space_id: str, device_id: str) -> None:
    app._forwarding_supervisor.tailnets[space_id]["authority"]["deviceId"] = device_id


def test_roster_writes_follow_the_tailnet_authority(tmp_path: Path) -> None:
    app = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
    )
    try:
        created = ipc_org_create(app, {"org": "group-acme"})
        space_id = created["spaceId"]
        actor = org_cell_actors(app).for_org("group-acme")

        assert actor.upsert_member(_bob_write()).has_member(f"u.{BOB_UID}")

        _repin(app, space_id, "node-b")
        with pytest.raises(OrgNetworkError) as refused:
            actor.upsert_member(_bob_write())
        assert refused.value.code == "ORG_ACTION_NOT_AUTHORIZED"
        assert "'node-b'" in str(refused.value)
        assert "'group-acme'" in str(refused.value)
        assert refused.value.details == {}

        _repin(app, space_id, "node-a")
        assert actor.upsert_member(_bob_write()).has_member(f"u.{BOB_UID}")
    finally:
        app.close()


def test_roster_write_gate_tolerates_orgs_without_a_tailnet(tmp_path: Path) -> None:
    app = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
    )
    try:
        created = ipc_org_create(app, {"org": "group-acme"})
        app._forwarding_supervisor.tailnets.clear()

        _build_service(app).require_roster_write_authority(
            "group-acme", created["spaceId"]
        )
    finally:
        app.close()


def test_ipc_unbound_org_is_reported_refused_and_safely_deletable(
    tmp_path: Path,
) -> None:
    app = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
    )
    try:
        created = ipc_org_create(app, {"org": "group-acme"})
        space_id = created["spaceId"]
        runtime = cell_runtime(app)
        assert runtime is not None
        cell_id = keyed_cell_id("org", runtime.identity.authority, space_id)
        cell = runtime.lookup(cell_id)
        assert cell is not None
        runtime.retire(cell_id, expect_revision=cell.revision)

        view = ipc_org_network(app, {})
        assert {
            "org": "group-acme",
            "path": "cells/org",
            "reason": "org-cell-unbound",
            "value": "docs/guides/migration-oc7-org-lifecycle.md",
        } in view["dataProblems"]

        with pytest.raises(DaemonRequestError) as refused:
            ipc_org_invite(app, {"org": "group-acme", "user": f"user:{BOB_OWNER}"})
        assert refused.value.code == "ORG_CELL_UNBOUND"
        assert "migration-oc7-org-lifecycle.md" in str(refused.value)
        assert refused.value.data is None

        preview = ipc_org_delete(app, {"org": "group-acme"})
        assert preview["phase"] == "pending-confirmation"
        deleted = ipc_org_delete(
            app,
            {
                "org": "group-acme",
                "confirmed": True,
                "operationId": preview["operationId"],
            },
        )
        assert deleted["phase"] == "complete"
    finally:
        app.close()


def _sub_org_app(tmp_path: Path) -> tuple[_OrgApp, dict]:
    app = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
    )
    root = ipc_org_create(app, {"org": "group-acme"})
    app._require_orgfs_runtime().facade.invite(
        root["spaceId"], f"user:{BOB_OWNER}", mode="rw"
    )
    org_cell_actors(app).for_org("group-acme").upsert_member(_bob_write())
    return app, ipc_org_create(app, {"org": "group-dept", "parent": "group-acme"})


def test_sub_org_roster_writes_follow_the_inherited_tailnet_authority(
    tmp_path: Path,
) -> None:
    app, created = _sub_org_app(tmp_path)
    try:
        tailnet_id = created["tailnetId"]
        actor = org_cell_actors(app).for_org("group-dept")
        assert actor.upsert_member(_bob_write()).has_member(f"u.{BOB_UID}")

        _repin(app, tailnet_id, "node-b")
        with pytest.raises(OrgNetworkError) as refused:
            actor.upsert_member(_bob_write())
        assert refused.value.code == "ORG_ACTION_NOT_AUTHORIZED"
        assert "'node-b'" in str(refused.value)
        assert "'group-dept'" in str(refused.value)

        _repin(app, tailnet_id, "node-a")
        assert actor.upsert_member(_bob_write()).has_member(f"u.{BOB_UID}")
    finally:
        app.close()


def test_sub_org_member_removal_is_refused_off_the_inherited_authority(
    tmp_path: Path,
) -> None:
    app, created = _sub_org_app(tmp_path)
    try:
        _repin(app, created["tailnetId"], "node-b")
        params = {"org": "group-dept", "user": f"user:{BOB_OWNER}"}

        with pytest.raises(DaemonRequestError) as refused:
            ipc_org_remove(app, params)
        assert refused.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        _repin(app, created["tailnetId"], "node-a")
        preview = ipc_org_remove(app, params)
        assert preview["phase"] == "pending-confirmation"
        _repin(app, created["tailnetId"], "node-b")
        with pytest.raises(DaemonRequestError) as refused_confirm:
            ipc_org_remove(
                app,
                {**params, "confirmed": True, "operationId": preview["operationId"]},
            )
        assert refused_confirm.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    finally:
        app.close()


def test_sub_org_roster_write_gate_fails_closed_without_the_inherited_row(
    tmp_path: Path,
) -> None:
    app, created = _sub_org_app(tmp_path)
    try:
        app._forwarding_supervisor.tailnets.pop(created["tailnetId"])

        with pytest.raises(OrgNetworkError) as refused:
            _build_service(app).require_roster_write_authority(
                "group-dept", created["spaceId"]
            )
        assert refused.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    finally:
        app.close()
