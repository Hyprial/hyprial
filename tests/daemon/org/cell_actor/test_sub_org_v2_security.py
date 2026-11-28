# Decision notes (OC6 V2 security regression suite):
# - Verifies fail-closed parent ownership and active tailnet authority checks on
#   sub-org create and repair (P1-1), non-reparentability of incomplete/unmarked
#   top-level directory spaces (P2-1), and depth >= 2 OIDC ancestor claim checks
#   on sub-org join (P2-2).
"""OC6 V2 critic regression tests for sub-org authorization, top-level lock, and OIDC chain validation."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import uuid

import pytest

from hyprial.daemon.impl.composition.cells import cell_runtime
from hyprial.daemon.impl.org.cell_actor.sub_org import resolve_org_parent_param
from hyprial.daemon.impl.org.network import state as org_state
from hyprial.daemon.impl.org.network.create import OrgCreateResumeDenied, prepare_create
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.org.network.ipc import (
    ipc_org_create,
    ipc_org_invite,
    ipc_org_join,
    ipc_org_repair,
)
from hyprial.daemon.impl.org.network.service import OrgNetworkError
from hyprial.identity import ORG_META_DOC
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
from tests.daemon.org.cell_actor.test_sub_org import (
    CAROL_UID,
    _confirm_in_org,
    _network_orgs,
    _seed_root_membership,
)
from tests.daemon.org.cell_actor.replica_support import link_replicas
from tests.support.tailnet_membership import FakeMembershipSidecar

ALICE_DEVICE_UID_2 = "d" * 12 + "4" + "d" * 3 + "8" + "d" * 15


def test_non_owner_member_cannot_create_or_repair_sub_org_or_forge_parent_binding(
    tmp_path: Path,
) -> None:
    """P1-1a: non-owner member cannot create/repair a sub-org or forge org.parent binding."""
    sidecar = FakeMembershipSidecar(owner=ALICE_OWNER, device_id="node-a", now_ms=None)
    app_a = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
        sidecar=sidecar,
    )
    app_b = _OrgApp(
        tmp_path,
        owner=BOB_OWNER,
        user_id=BOB_UID,
        node_id="node-b",
        device_uid=BOB_DEVICE_UID,
        sidecar=sidecar.for_device(BOB_OWNER, "node-b"),
    )
    link_replicas(app_a, app_b)
    try:
        root = ipc_org_create(app_a, {"org": "group-root"})
        root_space = root["spaceId"]
        _seed_root_membership(app_a, "group-root")

        root_inv = ipc_org_invite(app_a, {"org": "group-root", "user": f"user:{BOB_OWNER}"})
        app_b.oidc_roster = {f"u.{BOB_UID}": {"username": BOB_OWNER}}
        ipc_org_join(app_b, {"link": root_inv["link"]})
        _confirm_in_org(app_a, "group-root", BOB_OWNER, BOB_UID, BOB_DEVICE_UID)
        app_b.oidc_roster = dict(app_a.oidc_roster)

        assert getattr(app_b, "_cell_runtime", None) is None
        with pytest.raises(DaemonRequestError) as create_err:
            ipc_org_create(app_b, {"org": "group-bob-sub", "parent": "group-root"})
        assert create_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        bob_facade = app_b._require_orgfs_runtime().facade
        assert not any(sp.name == "group-bob-sub" for sp in bob_facade.spaces())
        assert org_state.org_create_intent(app_b.hyprial_home, "group-bob-sub") is None
        assert getattr(app_b, "_cell_runtime", None) is None

        app_b.network_isolated = True
        with pytest.raises(DaemonRequestError) as iso_err:
            ipc_org_create(app_b, {"org": "group-bob-sub-iso", "parent": "group-root"})
        assert iso_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        runtime_b = cell_runtime(app_b)
        assert runtime_b is not None
        assert runtime_b.index().of_kind("org") == ()
        with pytest.raises(OrgNetworkError) as param_err:
            resolve_org_parent_param(
                app_b,
                runtime=runtime_b,
                org="group-bob-sub",
                space_id=str(uuid.uuid4()),
                parent_org="group-root",
            )
        assert param_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"
        assert runtime_b.index().of_kind("org") == ()

        app_b.network_isolated = False
        repair_space = str(uuid.uuid4())
        org_state.begin_org_create(
            app_b.hyprial_home,
            org="group-bob-repair",
            node_id="node-b",
            owner=f"user:{BOB_OWNER}",
            started_at="2026-11-23T00:00:00Z",
            parent="group-root",
            parent_space_id=root_space,
            tailnet_id=root_space,
        )
        org_state.update_org_create(
            app_b.hyprial_home, "group-bob-repair", directory_space_id=repair_space
        )
        bob_facade.create_space("group-bob-repair", space_id=repair_space)

        preview = ipc_org_repair(app_b, {"org": "group-bob-repair"})
        assert len(preview["candidates"]) == 1
        assert preview["candidates"][0]["repairable"] is False
        assert ipc_org_repair(app_b, {"org": "group-bob-repair", "apply": True})["repaired"] == []
        assert (
            runtime_b.lookup(keyed_cell_id("org", runtime_b.identity.authority, repair_space))
            is None
        )
    finally:
        app_b.close()
        app_a.close()


def test_non_authority_second_device_cannot_create_or_repair_sub_org(
    tmp_path: Path,
) -> None:
    """P1-1b: owner's non-authority second device fails closed with AUTHORITY_REQUIRED on create/repair."""
    sidecar = FakeMembershipSidecar(owner=ALICE_OWNER, device_id="node-a", now_ms=None)
    app_a = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
        sidecar=sidecar,
    )
    app_a2 = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a2",
        device_uid=ALICE_DEVICE_UID_2,
        sidecar=sidecar.for_device(ALICE_OWNER, "node-a2"),
    )
    link_replicas(app_a, app_a2)
    try:
        root = ipc_org_create(app_a, {"org": "group-root"})
        root_space = root["spaceId"]
        _seed_root_membership(app_a, "group-root")

        root_inv = ipc_org_invite(app_a, {"org": "group-root", "user": f"user:{ALICE_OWNER}"})
        app_a2.oidc_roster = dict(app_a.oidc_roster)
        ipc_org_join(app_a2, {"link": root_inv["link"]})

        status_a2 = app_a2._forwarding_supervisor.membership_request("membership-status", {})
        assert status_a2["tailnets"][0]["role"] == "member"
        assert status_a2["tailnets"][0]["authorityDeviceId"] == "node-a"

        with pytest.raises(DaemonRequestError) as create_err:
            ipc_org_create(app_a2, {"org": "group-a2-sub", "parent": "group-root"})
        assert create_err.value.code == "AUTHORITY_REQUIRED"

        facade_a2 = app_a2._require_orgfs_runtime().facade
        assert not any(sp.name == "group-a2-sub" for sp in facade_a2.spaces())
        assert org_state.org_create_intent(app_a2.hyprial_home, "group-a2-sub") is None

        runtime_a2 = cell_runtime(app_a2)
        assert runtime_a2 is not None
        with pytest.raises(OrgNetworkError) as param_err:
            resolve_org_parent_param(
                app_a2,
                runtime=runtime_a2,
                org="group-a2-sub",
                space_id=str(uuid.uuid4()),
                parent_org="group-root",
            )
        assert param_err.value.code == "AUTHORITY_REQUIRED"

        repair_space = str(uuid.uuid4())
        org_state.begin_org_create(
            app_a2.hyprial_home,
            org="group-a2-repair",
            node_id="node-a2",
            owner=f"user:{ALICE_OWNER}",
            started_at="2026-11-23T00:00:00Z",
            parent="group-root",
            parent_space_id=root_space,
            tailnet_id=root_space,
        )
        org_state.update_org_create(
            app_a2.hyprial_home, "group-a2-repair", directory_space_id=repair_space
        )
        facade_a2.create_space("group-a2-repair", space_id=repair_space)

        preview = ipc_org_repair(app_a2, {"org": "group-a2-repair"})
        assert len(preview["candidates"]) == 1
        assert preview["candidates"][0]["repairable"] is False
        assert ipc_org_repair(app_a2, {"org": "group-a2-repair", "apply": True})["repaired"] == []
        assert (
            runtime_a2.lookup(keyed_cell_id("org", runtime_a2.identity.authority, repair_space))
            is None
        )

        ok = ipc_org_create(app_a, {"org": "group-a1-sub", "parent": "group-root"})
        assert ok["parent"] == "group-root"
        assert ok["tailnetId"] == root_space
    finally:
        app_a2.close()
        app_a.close()


def test_incomplete_or_unmarked_top_level_space_cannot_be_reparented(
    tmp_path: Path,
) -> None:
    """P2-1: incomplete/unmarked top-level directory spaces cannot be reparented via --parent."""
    sidecar = FakeMembershipSidecar(owner=ALICE_OWNER, device_id="node-a", now_ms=None)
    app = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
        sidecar=sidecar,
    )
    try:
        root = ipc_org_create(app, {"org": "group-root"})
        root_space = root["spaceId"]
        _seed_root_membership(app, "group-root")
        facade = app._require_orgfs_runtime().facade
        store = OrgFsDirectoryStore(facade)
        runtime = cell_runtime(app)
        assert runtime is not None

        creating_space = facade.create_space("group-creating-top")
        store.put_org_meta("group-creating-top", {"state": "creating"})
        with pytest.raises(DaemonRequestError) as creating_err:
            ipc_org_create(app, {"org": "group-creating-top", "parent": "group-root"})
        assert creating_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"
        creating_meta = json.loads(facade.read_text(creating_space.space_id, ORG_META_DOC)[0])
        assert creating_meta == {"state": "creating"}
        assert org_state.org_create_intent(app.hyprial_home, "group-creating-top") is None

        partial_space = facade.create_space("group-partial-top")
        store.put_org_meta("group-partial-top", {"state": "partial"})
        with pytest.raises(DaemonRequestError) as partial_err:
            ipc_org_create(app, {"org": "group-partial-top", "parent": "group-root"})
        assert partial_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"
        partial_meta = json.loads(facade.read_text(partial_space.space_id, ORG_META_DOC)[0])
        assert partial_meta == {"state": "partial"}
        assert org_state.org_create_intent(app.hyprial_home, "group-partial-top") is None

        legacy_space = facade.create_space("group-legacy-top").space_id
        sidecar.seed(legacy_space)
        with pytest.raises(DaemonRequestError) as legacy_err:
            ipc_org_create(app, {"org": "group-legacy-top", "parent": "group-root"})
        assert legacy_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"
        assert org_state.org_create_intent(app.hyprial_home, "group-legacy-top") is None

        self_tn_space = facade.create_space("group-self-tailnet").space_id
        sidecar.seed(self_tn_space)
        store.put_org_meta(
            "group-self-tailnet",
            {
                "state": "creating",
                "parent": "group-root",
                "parentSpaceId": root_space,
                "tailnetId": root_space,
            },
        )
        with pytest.raises(DaemonRequestError) as self_tn_err:
            ipc_org_create(app, {"org": "group-self-tailnet", "parent": "group-root"})
        assert self_tn_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"
        repair_preview = ipc_org_repair(app, {"org": "group-self-tailnet"})
        assert repair_preview["candidates"][0]["repairable"] is False
        with pytest.raises(OrgNetworkError) as self_param_err:
            resolve_org_parent_param(
                app,
                runtime=runtime,
                org="group-self-tailnet",
                space_id=self_tn_space,
                parent_org="group-root",
            )
        assert self_param_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        with pytest.raises(OrgCreateResumeDenied):
            prepare_create(
                fs=facade,
                home=app.hyprial_home,
                node_id="node-a",
                owner=ALICE_OWNER,
                org="group-creating-top",
                existing=creating_space,
                acl_space=None,
                binding_reason="missing-acl-binding",
                now=lambda: datetime.now(UTC),
                log=lambda *_args, **_kwargs: None,
                parent="group-root",
                parent_space_id=root_space,
                tailnet_id=root_space,
            )

        facade.create_space("group-sub-meta-only")
        store.put_org_meta(
            "group-sub-meta-only",
            {
                "state": "creating",
                "parent": "group-root",
                "parentSpaceId": root_space,
                "tailnetId": root_space,
            },
        )
        resumed = ipc_org_create(app, {"org": "group-sub-meta-only", "parent": "group-root"})
        assert resumed["parent"] == "group-root"
        assert resumed["tailnetId"] == root_space
    finally:
        app.close()


def test_depth_two_sub_org_join_validates_oidc_issuer_and_sub_across_all_ancestors(
    tmp_path: Path,
) -> None:
    """P2-2: depth-2 sub-org join checks OIDC issuer and sub across every ancestor org."""
    sidecar = FakeMembershipSidecar(owner=ALICE_OWNER, device_id="node-a", now_ms=None)
    app_a = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
        sidecar=sidecar,
    )
    app_b = _OrgApp(
        tmp_path,
        owner=BOB_OWNER,
        user_id=BOB_UID,
        node_id="node-b",
        device_uid=BOB_DEVICE_UID,
        sidecar=sidecar.for_device(BOB_OWNER, "node-b"),
    )
    link_replicas(app_a, app_b)
    try:
        ipc_org_create(app_a, {"org": "group-root"})
        _seed_root_membership(app_a, "group-root")
        ipc_org_create(app_a, {"org": "group-dept", "parent": "group-root"})
        _seed_root_membership(app_a, "group-dept")
        ipc_org_create(app_a, {"org": "group-team", "parent": "group-dept"})

        root_inv = ipc_org_invite(app_a, {"org": "group-root", "user": f"user:{BOB_OWNER}"})
        app_b.oidc_roster = {f"u.{BOB_UID}": {"username": BOB_OWNER}}
        ipc_org_join(app_b, {"link": root_inv["link"]})
        _confirm_in_org(app_a, "group-root", BOB_OWNER, BOB_UID, BOB_DEVICE_UID)

        dept_inv = ipc_org_invite(app_a, {"org": "group-dept", "user": f"user:{BOB_OWNER}"})
        ipc_org_join(app_b, {"link": dept_inv["link"]})
        _confirm_in_org(app_a, "group-dept", BOB_OWNER, BOB_UID, BOB_DEVICE_UID)

        team_inv = ipc_org_invite(app_a, {"org": "group-team", "user": f"user:{BOB_OWNER}"})

        issuers = {
            "group-root": app_b.user_issuer,
            "group-dept": app_b.user_issuer,
            "group-team": app_b.user_issuer,
        }
        rosters = {
            name: {f"u.{BOB_UID}": {"username": BOB_OWNER}}
            for name in ("group-root", "group-dept", "group-team")
        }
        app_b.authenticated_oidc_issuer = lambda org: issuers.get(org)
        app_b.authenticated_oidc_roster = lambda org: rosters.get(org)

        issuers["group-root"] = "https://other-idp.example.com"
        with pytest.raises(DaemonRequestError) as iss_err:
            ipc_org_join(app_b, {"link": team_inv["link"]})
        assert iss_err.value.code == "IDENTITY_CONFLICT"
        assert iss_err.value.data == {"conflictField": "issuer"}
        assert "group-team" not in _network_orgs(app_b)

        issuers["group-root"] = app_b.user_issuer
        rosters["group-root"] = {f"u.{CAROL_UID}": {"username": BOB_OWNER}}
        with pytest.raises(DaemonRequestError) as sub_err:
            ipc_org_join(app_b, {"link": team_inv["link"]})
        assert sub_err.value.code == "IDENTITY_CONFLICT"
        assert sub_err.value.data == {"conflictField": "sub"}
        assert "group-team" not in _network_orgs(app_b)

        rosters["group-root"] = {f"u.{BOB_UID}": {"username": BOB_OWNER}}
        joined = ipc_org_join(app_b, {"link": team_inv["link"]})
        assert joined["org"] == "group-team"
    finally:
        app_b.close()
        app_a.close()
