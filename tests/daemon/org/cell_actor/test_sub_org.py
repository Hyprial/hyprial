"""OC6: Sub-orgs (J18, J19, J24, O16) end-to-end cell, actor, tailnet, and convergence tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from types import SimpleNamespace
import uuid

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.composition.cells import cell_runtime
from hyprial.daemon.impl.org.cell_actor import (
    OrgMemberWrite,
    ensure_owner_org_cell,
    org_cell_actors,
    retire_owner_org_cell,
)
from hyprial.daemon.impl.org.cell_actor.sub_org import resolve_org_parent_param
from hyprial.daemon.impl.org.network import state as org_state
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.org.network.service import OrgNetworkError
from hyprial.daemon.impl.org.network.ipc import (
    ipc_org_create,
    ipc_org_invite,
    ipc_org_join,
    ipc_org_leave,
    ipc_org_list,
    ipc_org_remove,
    ipc_org_repair,
    process_leave_requests_for_app,
)
from hyprial.daemon.impl.org.membership.context import for_app
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.request_catalog.org import ORG_LIST_SPEC
from hyprial.identity import ORG_META_DOC
from hyprial.identity.impl.org_directory import decode_invite
from hyprial.kernel import DaemonRequestError, keyed_cell_id
from tests.daemon.cells.runtime.cell_runtime_support import uid_for
from tests.daemon.org.cell_actor.test_org_cell_actor import (
    ALICE_DEVICE_UID,
    ALICE_OWNER,
    ALICE_UID,
    BOB_DEVICE_UID,
    BOB_OWNER,
    BOB_UID,
    _OrgApp,
)
from tests.support.tailnet_membership import FakeMembershipSidecar, confirm_ipc_removal

CAROL_OWNER = "carol@example.com"
CAROL_UID = uid_for(f"user:{CAROL_OWNER}")
CAROL_DEVICE_UID = "c" * 12 + "4" + "c" * 3 + "8" + "c" * 15


def _bridge_member_orgfs(owner_app: _OrgApp, member_app: _OrgApp) -> None:
    """Connect member_app's LocalOrgFs to read/join spaces committed on owner_app."""
    owner_fs = owner_app._require_orgfs_runtime().facade
    member_fs = member_app._require_orgfs_runtime().facade

    def bridged_join(sid: str, timeout: float = 0.0) -> Any:
        src = owner_fs._spaces[sid]
        member_fs._spaces[sid] = src
        return src

    member_fs.join = bridged_join
    member_fs.members = lambda sid: owner_fs.members(sid)
    member_fs.space_meta = lambda sid: owner_fs.space_meta(sid)
    member_fs.read_text = lambda sid, path: owner_fs.read_text(sid, path)
    member_fs.listdir = lambda sid, path: owner_fs.listdir(sid, path)
    member_fs.document_authors = lambda sid, path: owner_fs.document_authors(sid, path)


def _network_orgs(app: _OrgApp) -> dict[str, dict[str, Any]]:
    """Return org entries keyed by org name from org.list."""
    return {item["org"]: item for item in ipc_org_list(app, {})["orgs"]}


def _confirm_in_org(
    app: _OrgApp,
    org: str,
    user_owner: str,
    user_uid: str,
    device_uid: str,
    *,
    invited_by: str = f"u.{ALICE_UID}",
    joined_at_ms: int = 2_000,
) -> None:
    """Confirm user in both OrgFS space access and parent org cell roster."""
    roster = dict(app.oidc_roster or {})
    roster[f"user:{user_owner}"] = {"sub": user_uid, "preferred_username": user_owner}
    app.oidc_roster = roster
    space_id = _network_orgs(app)[org]["spaceId"]
    app._require_orgfs_runtime().facade.invite(space_id, f"user:{user_owner}", mode="rw")
    org_cell_actors(app).for_org(org).upsert_member(
        OrgMemberWrite(
            uid_ref=f"u.{user_uid}",
            username=user_owner,
            role="member",
            invited_by=invited_by,
            joined_at_ms=joined_at_ms,
            devices=(
                {
                    "deviceUid": f"d.{device_uid}",
                    "keyId": None,
                    "publicKey": None,
                    "verifiedAtMs": joined_at_ms,
                },
            ),
        )
    )


def _seed_root_membership(app: _OrgApp, org: str) -> str:
    """Ensure local owner is present in oidc_roster for the given org."""
    app.oidc_roster = {
        f"user:{app.owner}": {"sub": app.user_id, "preferred_username": app.owner}
    }
    return str(_network_orgs(app)[org]["spaceId"])


def test_sub_org_inherits_parent_tailnet_with_separate_orgfs_space_and_nesting(
    tmp_path: Path,
) -> None:
    """Sub-org and nested grandchild reuse parent tailnetId without calling membership-init."""
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

        dept = ipc_org_create(app, {"org": "group-dept", "parent": "group-root"})
        dept_space = dept["spaceId"]
        _seed_root_membership(app, "group-dept")

        team = ipc_org_create(app, {"org": "group-team", "parent": "group-dept"})
        team_space = team["spaceId"]

        assert len({root_space, dept_space, team_space}) == 3
        assert root["aclSpaceId"] != dept["aclSpaceId"]
        assert dept["aclSpaceId"] != team["aclSpaceId"]
        assert dept["parent"] == "group-root"
        assert dept["tailnetId"] == root_space
        assert team["parent"] == "group-dept"
        assert team["tailnetId"] == root_space

        init_calls = [params for op, params in sidecar.calls if op == "membership-init"]
        assert [call["tailnetId"] for call in init_calls] == [root_space]

        runtime = cell_runtime(app)
        assert runtime is not None
        root_cell = runtime.lookup(keyed_cell_id("org", runtime.identity.authority, root_space))
        dept_cell = runtime.lookup(keyed_cell_id("org", runtime.identity.authority, dept_space))
        team_cell = runtime.lookup(keyed_cell_id("org", runtime.identity.authority, team_space))
        assert root_cell is not None and dept_cell is not None and team_cell is not None
        assert root_cell.param("org.parent") is None
        assert root_cell.param("org.tailnet") == root_space
        assert dept_cell.param("org.parent") == {
            "cellId": root_cell.cell_id,
            "directorySpace": root_space,
            "owner": f"u.{ALICE_UID}",
        }
        assert dept_cell.param("org.tailnet") == root_space
        assert team_cell.param("org.parent") == {
            "cellId": dept_cell.cell_id,
            "directorySpace": dept_space,
            "owner": f"u.{ALICE_UID}",
        }
        assert team_cell.param("org.tailnet") == root_space

        listed = {item["org"]: item for item in ipc_org_list(app, {})["orgs"]}
        assert listed["group-dept"]["parent"] == "group-root"
        assert listed["group-dept"]["tailnetId"] == root_space
        assert listed["group-dept"]["authorityDevice"] == listed["group-root"]["authorityDevice"]
        assert listed["group-team"]["parent"] == "group-dept"
        assert listed["group-team"]["tailnetId"] == root_space

        published = for_app(app).listing()
        errors = [e.message for e in Draft202012Validator(ORG_LIST_SPEC.result_schema).iter_errors(published)]
        assert errors == []
        rows = {item["org"]: item for item in published["orgs"]}
        assert rows["group-team"]["parent"] == "group-dept"
        assert rows["group-team"]["tailnetId"] == root_space
    finally:
        app.close()


def test_sub_org_creation_rejects_missing_deleted_and_self_parent(tmp_path: Path) -> None:
    """Creating a sub-org fails closed on unknown, self, or deleted parent."""
    app = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
    )
    try:
        with pytest.raises(DaemonRequestError) as missing:
            ipc_org_create(app, {"org": "group-child", "parent": "group-missing"})
        assert missing.value.code == "unknown-space"

        root_info = ipc_org_create(app, {"org": "group-root"})
        with pytest.raises(DaemonRequestError) as self_err:
            ipc_org_create(app, {"org": "group-root", "parent": "group-root"})
        assert self_err.value.code == "INVALID_ARGUMENT"

        OrgFsDirectoryStore(app._require_orgfs_runtime().facade).put_org_meta(
            "group-root", {"state": "deleted"}
        )
        retire_owner_org_cell(app, org="group-root", space_id=root_info["spaceId"])
        with pytest.raises(DaemonRequestError) as deleted_err:
            ipc_org_create(app, {"org": "group-child", "parent": "group-root"})
        assert deleted_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    finally:
        app.close()


def test_sub_org_membership_requires_parent_and_is_non_transitive(tmp_path: Path) -> None:
    """J24: non-parent member refused on invite/join/upsert; sub-org membership does not propagate up or sideways."""
    sidecar = FakeMembershipSidecar(owner=ALICE_OWNER, device_id="node-a", now_ms=None)
    app_a = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
        sidecar=sidecar,
    )
    bob_runtime = SimpleNamespace(facade=LocalOrgFs(author=f"user:{BOB_OWNER}"))
    app_b = _OrgApp(
        tmp_path,
        owner=BOB_OWNER,
        user_id=BOB_UID,
        node_id="node-b",
        device_uid=BOB_DEVICE_UID,
        runtime=bob_runtime,
        sidecar=sidecar.for_device(BOB_OWNER, "node-b"),
    )
    _bridge_member_orgfs(app_a, app_b)
    try:
        root = ipc_org_create(app_a, {"org": "group-root"})
        root_space = root["spaceId"]
        _seed_root_membership(app_a, "group-root")

        dept_a = ipc_org_create(app_a, {"org": "group-dept-a", "parent": "group-root"})
        dept_b = ipc_org_create(app_a, {"org": "group-dept-b", "parent": "group-root"})

        with pytest.raises(DaemonRequestError) as invite_denied:
            ipc_org_invite(app_a, {"org": "group-dept-a", "user": f"user:{BOB_OWNER}"})
        assert invite_denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        with pytest.raises(OrgNetworkError) as upsert_denied:
            org_cell_actors(app_a).for_org("group-dept-a").upsert_member(
                OrgMemberWrite(
                    uid_ref=f"u.{BOB_UID}",
                    username=BOB_OWNER,
                    role="member",
                    invited_by=f"u.{ALICE_UID}",
                    joined_at_ms=2_000,
                )
            )
        assert upsert_denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        root_invite = ipc_org_invite(app_a, {"org": "group-root", "user": f"user:{BOB_OWNER}"})
        app_a.oidc_roster[f"user:{BOB_OWNER}"] = {
            "sub": BOB_UID,
            "preferred_username": BOB_OWNER,
        }
        app_b.oidc_roster = dict(app_a.oidc_roster)
        with pytest.raises(DaemonRequestError) as unconfirmed_root_denied:
            ipc_org_invite(app_a, {"org": "group-dept-a", "user": f"user:{BOB_OWNER}"})
        assert unconfirmed_root_denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        with pytest.raises(DaemonRequestError) as pre_join_denied:
            ipc_org_invite(app_a, {"org": "group-dept-a", "user": f"user:{CAROL_OWNER}"})
        assert pre_join_denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        _confirm_in_org(app_a, "group-root", BOB_OWNER, BOB_UID, BOB_DEVICE_UID)
        sub_invite_before_bob_joins_root = ipc_org_invite(
            app_a, {"org": "group-dept-a", "user": f"user:{BOB_OWNER}"}
        )
        parsed = decode_invite(sub_invite_before_bob_joins_root["link"])
        assert parsed.parent_org == "group-root"
        assert parsed.tailnet is not None
        assert parsed.tailnet["tailnetId"] == root_space

        with pytest.raises(DaemonRequestError) as join_denied:
            ipc_org_join(app_b, {"link": sub_invite_before_bob_joins_root["link"]})
        assert join_denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        ipc_org_join(app_b, {"link": root_invite["link"]})
        sub_invite_ok = ipc_org_invite(
            app_a, {"org": "group-dept-a", "user": f"user:{BOB_OWNER}"}
        )
        assert root_invite["link"] != sub_invite_ok["link"]
        joined = ipc_org_join(app_b, {"link": sub_invite_ok["link"]})
        assert joined["org"] == "group-dept-a"

        org_cell_actors(app_a).for_org("group-dept-a").upsert_member(
            OrgMemberWrite(
                uid_ref=f"u.{BOB_UID}",
                username=BOB_OWNER,
                role="admin",
                invited_by=f"u.{ALICE_UID}",
                joined_at_ms=3_000,
                devices=(
                    {
                        "deviceUid": f"d.{BOB_DEVICE_UID}",
                        "keyId": None,
                        "publicKey": None,
                        "verifiedAtMs": 3_000,
                    },
                ),
            )
        )

        root_roles = {
            m.uid_ref: m.role for m in org_cell_actors(app_a).for_org("group-root").roster().members
        }
        assert root_roles[f"u.{BOB_UID}"] == "member"

        sibling_roster = org_cell_actors(app_a).for_org("group-dept-b").roster()
        assert sibling_roster is not None
        assert f"u.{BOB_UID}" not in {m.uid_ref for m in sibling_roster.members}

        with pytest.raises(DaemonRequestError) as sibling_join:
            ipc_org_invite(app_a, {"org": "group-dept-b", "user": f"user:{CAROL_OWNER}"})
        assert sibling_join.value.code == "ORG_ACTION_NOT_AUTHORIZED"
        assert dept_a["spaceId"] != dept_b["spaceId"]
    finally:
        app_b.close()
        app_a.close()


def test_parent_removal_and_leave_converge_without_auto_pruning_or_revoking_on_sub_org_only(
    tmp_path: Path,
) -> None:
    """O16: removing from sub-org leaves shared tailnet intact; removing/leaving parent flags out_of_parent_members without pruning child roster and blocks new child actions."""
    sidecar = FakeMembershipSidecar(owner=ALICE_OWNER, device_id="node-a", now_ms=None)
    app = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
        sidecar=sidecar,
    )
    bob_runtime = SimpleNamespace(facade=LocalOrgFs(author=f"user:{BOB_OWNER}"))
    app_b = _OrgApp(
        tmp_path,
        owner=BOB_OWNER,
        user_id=BOB_UID,
        node_id="node-b",
        device_uid=BOB_DEVICE_UID,
        runtime=bob_runtime,
        sidecar=sidecar.for_device(BOB_OWNER, "node-b"),
    )
    _bridge_member_orgfs(app, app_b)
    try:
        root = ipc_org_create(app, {"org": "group-root"})
        root_space = root["spaceId"]
        _seed_root_membership(app, "group-root")
        root_inv = ipc_org_invite(app, {"org": "group-root", "user": f"user:{BOB_OWNER}"})
        app.oidc_roster[f"user:{BOB_OWNER}"] = {
            "sub": BOB_UID,
            "preferred_username": BOB_OWNER,
        }
        app_b.oidc_roster = dict(app.oidc_roster)
        ipc_org_join(app_b, {"link": root_inv["link"]})
        _confirm_in_org(app, "group-root", BOB_OWNER, BOB_UID, BOB_DEVICE_UID)

        ipc_org_create(app, {"org": "group-dept", "parent": "group-root"})
        dept_inv = ipc_org_invite(app, {"org": "group-dept", "user": f"user:{BOB_OWNER}"})
        ipc_org_join(app_b, {"link": dept_inv["link"]})
        _confirm_in_org(app, "group-dept", BOB_OWNER, BOB_UID, BOB_DEVICE_UID)

        sub_preview = ipc_org_remove(app, {"org": "group-dept", "user": f"user:{BOB_OWNER}"})
        assert sub_preview["requiresConfirmation"] is True
        sub_removed = ipc_org_remove(
            app,
            {
                "org": "group-dept",
                "user": f"user:{BOB_OWNER}",
                "confirmed": True,
                "operationId": sub_preview["operationId"],
                "deviceIds": list(sub_preview["deviceIds"]),
            },
        )
        assert sub_removed["phase"] == "complete"
        assert sub_removed["tailnetUnchanged"] is True
        revoke_calls = [params for op, params in sidecar.calls if op == "member-revoke"]
        assert revoke_calls == []

        root_actor = org_cell_actors(app).for_org("group-root")
        root_roster = root_actor.roster()
        assert root_roster is not None
        assert f"u.{BOB_UID}" in {m.uid_ref for m in root_roster.members}

        _confirm_in_org(app, "group-dept", BOB_OWNER, BOB_UID, BOB_DEVICE_UID)

        confirm_ipc_removal(ipc_org_remove, app, {"org": "group-root", "user": f"user:{BOB_OWNER}"})
        revoke_calls_after_parent = [
            params for op, params in sidecar.calls if op == "member-revoke"
        ]
        assert [c["tailnetId"] for c in revoke_calls_after_parent] == [root_space]

        dept_actor = org_cell_actors(app).for_org("group-dept")
        dept_snap = dept_actor.snapshot()
        dept_roster = dept_actor.roster()
        assert dept_roster is not None
        member_by_uid = {m.uid_ref: m for m in dept_roster.members}
        assert f"u.{BOB_UID}" in member_by_uid
        assert member_by_uid[f"u.{BOB_UID}"].in_parent is False
        assert f"u.{BOB_UID}" in dept_roster.out_of_parent_members
        assert f"u.{BOB_UID}" in dept_snap.out_of_parent_members
        assert dept_snap.parent_missing is False

        with pytest.raises(DaemonRequestError) as post_remove_invite:
            ipc_org_invite(app, {"org": "group-dept", "user": f"user:{BOB_OWNER}"})
        assert post_remove_invite.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        OrgFsDirectoryStore(app._require_orgfs_runtime().facade).put_org_meta(
            "group-root", {"state": "deleted"}
        )
        retire_owner_org_cell(app, org="group-root", space_id=root_space)
        after_delete_snap = dept_actor.snapshot()
        assert after_delete_snap.parent_missing is True

        with pytest.raises(DaemonRequestError) as nested_blocked:
            ipc_org_create(app, {"org": "group-team", "parent": "group-dept"})
        assert nested_blocked.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    finally:
        app_b.close()
        app.close()


def test_sub_org_leave_and_repair_preserve_inherited_tailnet(tmp_path: Path) -> None:
    """Member leaving sub-org stays on parent tailnet; repairing interrupted sub-org skips membership-init."""
    sidecar = FakeMembershipSidecar(owner=ALICE_OWNER, device_id="node-a", now_ms=None)
    app_a = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
        sidecar=sidecar,
    )
    bob_runtime = SimpleNamespace(facade=LocalOrgFs(author=f"user:{BOB_OWNER}"))
    app_b = _OrgApp(
        tmp_path,
        owner=BOB_OWNER,
        user_id=BOB_UID,
        node_id="node-b",
        device_uid=BOB_DEVICE_UID,
        runtime=bob_runtime,
        sidecar=sidecar.for_device(BOB_OWNER, "node-b"),
    )
    _bridge_member_orgfs(app_a, app_b)
    try:
        root = ipc_org_create(app_a, {"org": "group-root"})
        root_space = root["spaceId"]
        _seed_root_membership(app_a, "group-root")
        root_inv = ipc_org_invite(app_a, {"org": "group-root", "user": f"user:{BOB_OWNER}"})
        app_a.oidc_roster[f"user:{BOB_OWNER}"] = {
            "sub": BOB_UID,
            "preferred_username": BOB_OWNER,
        }
        app_b.oidc_roster = dict(app_a.oidc_roster)
        ipc_org_join(app_b, {"link": root_inv["link"]})
        _confirm_in_org(app_a, "group-root", BOB_OWNER, BOB_UID, BOB_DEVICE_UID)

        ipc_org_create(app_a, {"org": "group-dept", "parent": "group-root"})
        dept_inv = ipc_org_invite(app_a, {"org": "group-dept", "user": f"user:{BOB_OWNER}"})
        ipc_org_join(app_b, {"link": dept_inv["link"]})
        _confirm_in_org(app_a, "group-dept", BOB_OWNER, BOB_UID, BOB_DEVICE_UID)

        left = confirm_ipc_removal(ipc_org_leave, app_b, {"org": "group-dept"})
        assert left["phase"] == "complete"
        assert left["tailnetUnchanged"] is True
        app_a._require_orgfs_runtime().facade.document_covers_member_add = (
            lambda *_args, **_kwargs: True
        )
        processed = process_leave_requests_for_app(app_a)
        assert any(item["org"] == "group-dept" for item in processed["processed"])
        assert "group-root" in _network_orgs(app_b)
        assert "group-dept" not in _network_orgs(app_b)

        broken = "group-broken-sub"
        broken_space = str(uuid.uuid4())
        org_state.begin_org_create(
            app_a.hyprial_home,
            org=broken,
            node_id="node-a",
            owner=f"user:{ALICE_OWNER}",
            started_at="2026-11-23T00:00:00Z",
            parent="group-root",
            parent_space_id=root_space,
            tailnet_id=root_space,
        )
        org_state.update_org_create(
            app_a.hyprial_home,
            broken,
            directory_space_id=broken_space,
        )
        app_a._require_orgfs_runtime().facade.create_space(
            broken,
            space_id=broken_space,
        )

        before_init_count = len(
            [params for op, params in sidecar.calls if op == "membership-init"]
        )
        repaired = ipc_org_repair(app_a, {"apply": True})
        assert any(item["org"] == broken for item in repaired["repaired"])
        after_init_count = len(
            [params for op, params in sidecar.calls if op == "membership-init"]
        )
        assert after_init_count == before_init_count
        net = _network_orgs(app_a)[broken]
        assert net["parent"] == "group-root"
        assert net["tailnetId"] == root_space
    finally:
        app_b.close()
        app_a.close()


def test_member_readonly_lookup_does_not_initialize_cell_runtime_or_write_cell(
    tmp_path: Path,
) -> None:
    """P1-1: member read-only lookup never initializes cell runtime or writes owner cells."""
    sidecar = FakeMembershipSidecar(owner=ALICE_OWNER, device_id="node-a", now_ms=None)
    app_a = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
        sidecar=sidecar,
    )
    bob_runtime = SimpleNamespace(facade=LocalOrgFs(author=f"user:{BOB_OWNER}"))
    app_b = _OrgApp(
        tmp_path,
        owner=BOB_OWNER,
        user_id=BOB_UID,
        node_id="node-b",
        device_uid=BOB_DEVICE_UID,
        runtime=bob_runtime,
        sidecar=sidecar.for_device(BOB_OWNER, "node-b"),
    )
    _bridge_member_orgfs(app_a, app_b)
    try:
        root = ipc_org_create(app_a, {"org": "group-root"})
        _seed_root_membership(app_a, "group-root")
        root_inv = ipc_org_invite(app_a, {"org": "group-root", "user": f"user:{BOB_OWNER}"})
        app_a.oidc_roster[f"user:{BOB_OWNER}"] = {
            "sub": BOB_UID,
            "preferred_username": BOB_OWNER,
        }
        app_b.oidc_roster = dict(app_a.oidc_roster)
        ipc_org_join(app_b, {"link": root_inv["link"]})
        _confirm_in_org(app_a, "group-root", BOB_OWNER, BOB_UID, BOB_DEVICE_UID)

        ipc_org_create(app_a, {"org": "group-dept", "parent": "group-root"})
        dept_inv = ipc_org_invite(app_a, {"org": "group-dept", "user": f"user:{BOB_OWNER}"})

        app_b.oidc_roster = None
        app_b.authenticated_oidc_issuer = lambda _org: None
        assert getattr(app_b, "_cell_runtime", None) is None
        assert not (app_b.state_dir / "cells.sqlite3").exists()

        with pytest.raises(DaemonRequestError) as join_err:
            ipc_org_join(app_b, {"link": dept_inv["link"]})
        assert join_err.value.code == "IDENTITY_CONFLICT"
        assert getattr(app_b, "_cell_runtime", None) is None
        assert not (app_b.state_dir / "cells.sqlite3").exists()

        runtime_b = cell_runtime(app_b)
        assert runtime_b is not None
        assert runtime_b.index().of_kind("org") == ()

        with pytest.raises(DaemonRequestError) as join_err_with_runtime:
            ipc_org_join(app_b, {"link": dept_inv["link"]})
        assert join_err_with_runtime.value.code == "IDENTITY_CONFLICT"
        assert runtime_b.index().of_kind("org") == ()
        assert ensure_owner_org_cell(app_b, org="group-root", space_id=root["spaceId"]) is None
        assert runtime_b.index().of_kind("org") == ()
    finally:
        app_b.close()
        app_a.close()


def test_active_top_level_org_cannot_be_reparented_or_fork_cell_metadata(
    tmp_path: Path,
) -> None:
    """P1-2: reparenting an active top-level org fails closed without splitting metadata and cell."""
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
        ipc_org_create(app, {"org": "group-root"})
        _seed_root_membership(app, "group-root")
        plain = ipc_org_create(app, {"org": "group-plain"})
        plain_space = plain["spaceId"]

        with pytest.raises(DaemonRequestError) as reparent_err:
            ipc_org_create(app, {"org": "group-plain", "parent": "group-root"})
        assert reparent_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        raw_meta, _ver = app._require_orgfs_runtime().facade.read_text(plain_space, ORG_META_DOC)
        meta = json.loads(raw_meta)
        assert meta["state"] == "active"
        assert "parent" not in meta
        assert "parentSpaceId" not in meta
        assert "tailnetId" not in meta

        listed = _network_orgs(app)["group-plain"]
        assert "parent" not in listed
        assert "tailnetId" not in listed

        runtime = cell_runtime(app)
        assert runtime is not None
        plain_cell = runtime.lookup(keyed_cell_id("org", runtime.identity.authority, plain_space))
        assert plain_cell is not None
        assert plain_cell.param("org.parent") is None
        assert plain_cell.param("org.tailnet") == plain_space

        with pytest.raises(OrgNetworkError) as cell_err:
            ensure_owner_org_cell(
                app, org="group-plain", space_id=plain_space, parent="group-root"
            )
        assert cell_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        idempotent = ipc_org_create(app, {"org": "group-plain"})
        assert idempotent["spaceId"] == plain_space
        assert "parent" not in idempotent
    finally:
        app.close()


def test_repair_validates_parent_space_id_and_preserves_sub_org_fields(
    tmp_path: Path,
) -> None:
    """P1-3: org.repair validates parentSpaceId and tailnetId fail-closed and preserves them."""
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
        runtime = cell_runtime(app)
        assert runtime is not None

        ok_space = str(uuid.uuid4())
        org_state.begin_org_create(
            app.hyprial_home,
            org="group-sub-ok",
            node_id="node-a",
            owner=f"user:{ALICE_OWNER}",
            started_at="2026-11-23T00:00:00Z",
            parent="group-root",
            parent_space_id=root_space,
            tailnet_id=root_space,
        )
        org_state.update_org_create(
            app.hyprial_home, "group-sub-ok", directory_space_id=ok_space
        )
        facade.create_space("group-sub-ok", space_id=ok_space)

        preview_ok = ipc_org_repair(app, {"org": "group-sub-ok"})
        assert len(preview_ok["candidates"]) == 1
        cand_ok = preview_ok["candidates"][0]
        assert cand_ok["repairable"] is True
        assert cand_ok["parent"] == "group-root"
        assert cand_ok["parentSpaceId"] == root_space
        assert cand_ok["tailnetId"] == root_space

        applied_ok = ipc_org_repair(app, {"org": "group-sub-ok", "apply": True})
        assert len(applied_ok["repaired"]) == 1
        assert applied_ok["repaired"][0]["parent"] == "group-root"
        assert applied_ok["repaired"][0]["parentSpaceId"] == root_space
        assert applied_ok["repaired"][0]["tailnetId"] == root_space

        stale_parent_space = str(uuid.uuid4())
        stale_space = str(uuid.uuid4())
        org_state.begin_org_create(
            app.hyprial_home,
            org="group-sub-stale",
            node_id="node-a",
            owner=f"user:{ALICE_OWNER}",
            started_at="2026-11-23T00:00:00Z",
            parent="group-root",
            parent_space_id=stale_parent_space,
            tailnet_id=root_space,
        )
        org_state.update_org_create(
            app.hyprial_home, "group-sub-stale", directory_space_id=stale_space
        )
        facade.create_space("group-sub-stale", space_id=stale_space)

        preview_stale = ipc_org_repair(app, {"org": "group-sub-stale"})
        cand_stale = preview_stale["candidates"][0]
        assert cand_stale["repairable"] is False
        assert cand_stale["parent"] == "group-root"
        assert cand_stale["parentSpaceId"] == stale_parent_space
        assert cand_stale["tailnetId"] == root_space
        assert isinstance(cand_stale.get("error"), str) and cand_stale["error"]
        assert ipc_org_repair(app, {"org": "group-sub-stale", "apply": True})["repaired"] == []
        assert (
            runtime.lookup(keyed_cell_id("org", runtime.identity.authority, stale_space))
            is None
        )
        assert not any(sp.name == "group-sub-stale-acl" for sp in facade.spaces())
        with pytest.raises(DaemonRequestError) as stale_err:
            ipc_org_create(app, {"org": "group-sub-stale"})
        assert stale_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        missing_psid_space = str(uuid.uuid4())
        org_state.begin_org_create(
            app.hyprial_home,
            org="group-sub-missing-psid",
            node_id="node-a",
            owner=f"user:{ALICE_OWNER}",
            started_at="2026-11-23T00:00:00Z",
            parent="group-root",
            tailnet_id=root_space,
        )
        org_state.update_org_create(
            app.hyprial_home,
            "group-sub-missing-psid",
            directory_space_id=missing_psid_space,
        )
        facade.create_space("group-sub-missing-psid", space_id=missing_psid_space)

        preview_missing = ipc_org_repair(app, {"org": "group-sub-missing-psid"})
        cand_missing = preview_missing["candidates"][0]
        assert cand_missing["repairable"] is False
        assert cand_missing["parent"] == "group-root"
        assert cand_missing["parentSpaceId"] is None
        assert cand_missing["tailnetId"] == root_space
        assert isinstance(cand_missing.get("error"), str) and cand_missing["error"]
        assert (
            ipc_org_repair(app, {"org": "group-sub-missing-psid", "apply": True})["repaired"]
            == []
        )
        assert (
            runtime.lookup(
                keyed_cell_id("org", runtime.identity.authority, missing_psid_space)
            )
            is None
        )
        with pytest.raises(DaemonRequestError) as missing_err:
            ipc_org_create(app, {"org": "group-sub-missing-psid"})
        assert missing_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        other_space = str(uuid.uuid4())
        bad_tailnet_space = str(uuid.uuid4())
        org_state.begin_org_create(
            app.hyprial_home,
            org="group-sub-bad-tailnet",
            node_id="node-a",
            owner=f"user:{ALICE_OWNER}",
            started_at="2026-11-23T00:00:00Z",
            parent="group-root",
            parent_space_id=root_space,
            tailnet_id=other_space,
        )
        org_state.update_org_create(
            app.hyprial_home,
            "group-sub-bad-tailnet",
            directory_space_id=bad_tailnet_space,
        )
        facade.create_space("group-sub-bad-tailnet", space_id=bad_tailnet_space)

        preview_bad = ipc_org_repair(app, {"org": "group-sub-bad-tailnet"})
        cand_bad = preview_bad["candidates"][0]
        assert cand_bad["repairable"] is False
        assert cand_bad["parent"] == "group-root"
        assert cand_bad["parentSpaceId"] == root_space
        assert cand_bad["tailnetId"] == other_space
        assert isinstance(cand_bad.get("error"), str) and cand_bad["error"]
        assert (
            ipc_org_repair(app, {"org": "group-sub-bad-tailnet", "apply": True})["repaired"]
            == []
        )
        assert (
            runtime.lookup(
                keyed_cell_id("org", runtime.identity.authority, bad_tailnet_space)
            )
            is None
        )
        with pytest.raises(DaemonRequestError) as bad_err:
            ipc_org_create(app, {"org": "group-sub-bad-tailnet"})
        assert bad_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    finally:
        app.close()


def test_root_org_tailnet_invariant_enforced_in_network_and_cell_resolution(
    tmp_path: Path,
) -> None:
    """P1-4: root org must satisfy tailnetId == spaceId across metadata, cell, and sidecar."""
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
        other = ipc_org_create(app, {"org": "group-other"})
        other_space = other["spaceId"]
        _seed_root_membership(app, "group-root")

        facade = app._require_orgfs_runtime().facade
        OrgFsDirectoryStore(facade).put_org_meta(
            "group-root", {"state": "active", "tailnetId": other_space}
        )

        with pytest.raises(DaemonRequestError) as meta_err:
            ipc_org_create(app, {"org": "group-child-meta", "parent": "group-root"})
        assert meta_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        app.network_isolated = True
        with pytest.raises(DaemonRequestError) as isolated_err:
            ipc_org_create(app, {"org": "group-child-meta", "parent": "group-root"})
        assert isolated_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        app.network_isolated = False
        facade.write_text(root_space, ORG_META_DOC, '{"state":"active"}')

        runtime = cell_runtime(app)
        assert runtime is not None
        root_cell = runtime.lookup(keyed_cell_id("org", runtime.identity.authority, root_space))
        assert root_cell is not None

        class _TamperedCell:
            def __init__(self, inner: Any) -> None:
                self._inner = inner
                self.cell_id = inner.cell_id
                self.authority = inner.authority
                self.retired = inner.retired

            def param(self, key: str) -> Any:
                if key == "org.tailnet":
                    return other_space
                return self._inner.param(key)

        class _TamperedRuntime:
            def __init__(self, inner: Any) -> None:
                self._inner = inner
                self.identity = inner.identity

            def lookup(self, cell_id: str) -> Any:
                if cell_id == root_cell.cell_id:
                    return _TamperedCell(root_cell)
                return self._inner.lookup(cell_id)

        with pytest.raises(OrgNetworkError) as cell_err:
            resolve_org_parent_param(
                app,
                runtime=_TamperedRuntime(runtime),
                org="group-child-cell",
                space_id=str(uuid.uuid4()),
                parent_org="group-root",
            )
        assert cell_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        with pytest.raises(OrgNetworkError) as hint_err:
            resolve_org_parent_param(
                app,
                runtime=runtime,
                org="group-child-hint",
                space_id=str(uuid.uuid4()),
                parent_org="group-root",
                tailnet_hint=other_space,
            )
        assert hint_err.value.code == "ORG_ACTION_NOT_AUTHORIZED"

        sidecar.tailnets.pop(root_space, None)
        with pytest.raises(OrgNetworkError) as sidecar_err:
            resolve_org_parent_param(
                app,
                runtime=runtime,
                org="group-child-sidecar",
                space_id=str(uuid.uuid4()),
                parent_org="group-root",
            )
        assert sidecar_err.value.code == "MEMBERSHIP_NOT_INITIALIZED"
    finally:
        app.close()
