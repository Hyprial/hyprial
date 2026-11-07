"""Org IPC acceptance for ACL-space membership and policy custody."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from tests.support.tailnet_membership import confirm_ipc_removal

from hyprial.daemon.impl.org.network.ipc import (
    ipc_org_create,
    ipc_org_delete,
    ipc_org_invite,
    ipc_org_leave,
    ipc_org_network,
    ipc_org_remove,
)
from hyprial.daemon.impl.org.network.policy import ACL_POLICY_DOC
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.identity import ORG_META_DOC, default_role_grants_document
from hyprial.identity.impl.device import DeviceRecord, address_path, write_device_record
from hyprial.kernel import DaemonRequestError

from tests.support.tailnet_membership import FakeMembershipSidecar, key

OWNER = "owner@example"
BOB = "bob@example"
CAROL = "carol@example"


class _App:
    def __init__(self, runtime: OrgFsRuntime, home: Path, owner: str = OWNER) -> None:
        self._runtime = runtime
        self.hyprial_home = home
        self.node_id = "node-a"
        self.owner = owner
        if not hasattr(runtime, "membership_sidecar"):
            runtime.membership_sidecar = FakeMembershipSidecar(now_ms=None)
        self._forwarding_supervisor = runtime.membership_sidecar
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def _require_orgfs_runtime(self) -> OrgFsRuntime:
        return self._runtime

    def _start_forwarding_supervisor(self) -> None:
        return None

    def _reconcile_forwarding_endpoints(self) -> None:
        return None

    def _await_forwarding_reconcile(self, reason: str, *, timeout: float, **fields: Any) -> None:
        self._reconcile_forwarding_endpoints()

    def _request_forwarding_reconcile(self, reason: str, **fields: Any) -> bool:
        return True

    def _forwarding_status_json(self) -> dict[str, Any]:
        return {"policy": "auto", "peers": []}

    def _log(self, level: str, domain: str, event: str, **fields: Any) -> None:
        self.events.append((level, f"{domain}.{event}", fields))


@pytest.fixture
def apps(tmp_path: Path):
    runtime = OrgFsRuntime(
        tmp_path / "state", node_id="node-a", author=f"user:{OWNER}"
    )
    write_device_record(
        tmp_path,
        DeviceRecord(
            device_id="node-a",
            owner=OWNER,
            server_public=key("server-node-a"),
            client_public=key("client-node-a"),
        ),
    )
    address_path(tmp_path).write_text("tcp://10.0.0.9:7447#ipc-psk")
    yield _App(runtime, tmp_path), _App(runtime, tmp_path, BOB)
    runtime.close()


def _space_id(app: _App, name: str) -> str:
    return next(
        space.space_id
        for space in app._runtime.facade.spaces()
        if space.name == name
    )


def _member_modes(app: _App, name: str) -> dict[str, str]:
    return {
        member.user: member.mode
        for member in app._runtime.facade.members(_space_id(app, name))
    }


def test_invite_and_remove_sync_acl_membership_through_org_ipc(apps) -> None:
    owner, _bob = apps
    ipc_org_create(owner, {"org": "group-acme"})

    ipc_org_invite(owner, {"org": "group-acme", "user": f"user:{BOB}"})

    assert _member_modes(owner, "group-acme")[f"user:{BOB}"] == "rw"
    assert _member_modes(owner, "group-acme-acl")[f"user:{BOB}"] == "ro"

    removed = confirm_ipc_removal(ipc_org_remove, owner, {"org": "group-acme", "user": f"user:{BOB}"})
    assert removed["phase"] == "complete"

    assert f"user:{BOB}" not in _member_modes(owner, "group-acme")
    assert f"user:{BOB}" not in _member_modes(owner, "group-acme-acl")


def test_permission_decision_comes_from_acl_document_and_fails_closed(apps) -> None:
    owner, bob = apps
    created = ipc_org_create(owner, {"org": "group-acme"})
    ipc_org_invite(owner, {"org": "group-acme", "user": f"user:{BOB}"})
    ipc_org_invite(owner, {"org": "group-acme", "user": f"user:{CAROL}"})

    with pytest.raises(DaemonRequestError) as denied:
        ipc_org_remove(bob, {"org": "group-acme", "user": f"user:{CAROL}"})
    assert denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"

    document = default_role_grants_document()
    grants = document["grants"]
    assert isinstance(grants, list)
    grants.append(
        {"role": "member", "resource": "org:members", "action": "remove"}
    )
    owner._runtime.facade.write_text(
        created["aclSpaceId"],
        ACL_POLICY_DOC,
        json.dumps(document, sort_keys=True, separators=(",", ":")),
    )

    removed = confirm_ipc_removal(ipc_org_remove,
        bob, {"org": "group-acme", "user": f"user:{CAROL}"}
    )
    assert removed["user"] == f"user:{CAROL}"
    assert removed["phase"] == "complete"
    assert f"user:{CAROL}" not in _member_modes(owner, "group-acme")
    assert f"user:{CAROL}" not in _member_modes(owner, "group-acme-acl")

    ipc_org_invite(owner, {"org": "group-acme", "user": f"user:{CAROL}"})
    owner._runtime.facade.remove(created["aclSpaceId"], ACL_POLICY_DOC)
    with pytest.raises(DaemonRequestError) as fail_closed:
        ipc_org_remove(bob, {"org": "group-acme", "user": f"user:{CAROL}"})
    assert fail_closed.value.code == "ORG_ACTION_NOT_AUTHORIZED"

    # Both actions are granted by the generated default table. They must still
    # fail when the document is unavailable, proving there is no fallback.
    with pytest.raises(DaemonRequestError) as leave_denied:
        ipc_org_leave(bob, {"org": "group-acme"})
    assert leave_denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    assert ipc_org_network(bob, {})["unavailableOrgs"] == [
        {"org": "group-acme", "available": False, "reason": "policy-denied"}
    ]
    with pytest.raises(DaemonRequestError) as read_denied:
        ipc_org_network(bob, {"org": "group-acme"})
    assert read_denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    assert any(
        event == "org.policy.grants-unavailable"
        for _level, event, _fields in bob.events
    )


def test_creator_retry_resumes_after_directory_space_creation_crash(
    apps, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.org.network import service

    owner, _bob = apps
    facade = owner._runtime.facade
    original_update = service.update_org_create

    def interrupted_update(home, org, **values):
        if values.get("directory_space_id") is not None:
            raise RuntimeError("crash after directory space creation")
        return original_update(home, org, **values)

    monkeypatch.setattr(service, "update_org_create", interrupted_update)
    with pytest.raises(RuntimeError, match="crash after directory"):
        ipc_org_create(owner, {"org": "group-acme"})
    monkeypatch.setattr(service, "update_org_create", original_update)

    created = ipc_org_create(owner, {"org": "group-acme"})

    spaces = facade.spaces()
    assert [space.name for space in spaces].count("group-acme") == 1
    assert [space.name for space in spaces].count("group-acme-acl") == 1
    assert facade.space_meta(created["spaceId"])["aclSpaceId"] == created["aclSpaceId"]
    lifecycle, _version = facade.read_text(created["spaceId"], ORG_META_DOC)
    assert json.loads(lifecycle)["state"] == "active"


def test_creator_retry_reuses_acl_space_when_intent_update_crashes(
    apps, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.org.network.create import recovery

    owner, _bob = apps
    facade = owner._runtime.facade
    original_update = recovery.update_org_create

    def interrupted_update(home, org, **values):
        if values.get("acl_space_id") is not None:
            raise RuntimeError("crash after ACL space creation")
        return original_update(home, org, **values)

    monkeypatch.setattr(recovery, "update_org_create", interrupted_update)
    with pytest.raises(RuntimeError, match="crash after ACL"):
        ipc_org_create(owner, {"org": "group-acme"})
    monkeypatch.setattr(recovery, "update_org_create", original_update)

    created = ipc_org_create(owner, {"org": "group-acme"})

    spaces = facade.spaces()
    assert [space.name for space in spaces].count("group-acme") == 1
    assert [space.name for space in spaces].count("group-acme-acl") == 1
    assert facade.space_meta(created["spaceId"])["aclSpaceId"] == created["aclSpaceId"]


def test_creator_retry_finishes_directory_entry_after_acl_binding_crash(
    apps, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore

    owner, _bob = apps
    facade = owner._runtime.facade
    original_put = OrgFsDirectoryStore.put_org_meta

    def interrupted_put(self, org, record):
        raise RuntimeError("crash before directory lifecycle entry")

    monkeypatch.setattr(OrgFsDirectoryStore, "put_org_meta", interrupted_put)
    with pytest.raises(RuntimeError, match="crash before directory"):
        ipc_org_create(owner, {"org": "group-acme"})
    monkeypatch.setattr(OrgFsDirectoryStore, "put_org_meta", original_put)

    created = ipc_org_create(owner, {"org": "group-acme"})

    lifecycle, _version = facade.read_text(created["spaceId"], ORG_META_DOC)
    assert json.loads(lifecycle)["state"] == "active"
    assert [space.name for space in facade.spaces()].count("group-acme") == 1
    assert [space.name for space in facade.spaces()].count("group-acme-acl") == 1


def test_repair_recovers_bound_space_missing_grant_and_directory_entry(apps) -> None:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_repair

    owner, _bob = apps
    facade = owner._runtime.facade
    directory = facade.create_space("group-acme")
    acl = facade.create_space("group-acme-acl")
    facade.update_space_meta(directory.space_id, {"aclSpaceId": acl.space_id})

    preview = ipc_org_repair(owner, {})
    assert preview["candidates"] == [
        {
            "org": "group-acme",
            "spaceId": directory.space_id,
            "reason": "missing-acl-policy",
            "repairable": True,
        }
    ]

    applied = ipc_org_repair(owner, {"apply": True})

    assert applied["repaired"][0]["spaceId"] == directory.space_id
    assert applied["repaired"][0]["aclSpaceId"] == acl.space_id
    lifecycle, _version = facade.read_text(directory.space_id, ORG_META_DOC)
    assert json.loads(lifecycle)["state"] == "active"
    grants, _version = facade.read_text(acl.space_id, ACL_POLICY_DOC)
    assert json.loads(grants) == default_role_grants_document()


def test_repair_of_missing_directory_entry_preserves_existing_grants(apps) -> None:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_repair

    owner, _bob = apps
    facade = owner._runtime.facade
    directory = facade.create_space("group-acme")
    acl = facade.create_space("group-acme-acl")
    facade.update_space_meta(directory.space_id, {"aclSpaceId": acl.space_id})
    facade.mkdir(acl.space_id, "policy")
    custom_grants = default_role_grants_document()
    custom_grants["grants"] = custom_grants["grants"][:-1]
    encoded = json.dumps(custom_grants, sort_keys=True, separators=(",", ":"))
    facade.write_text(acl.space_id, ACL_POLICY_DOC, encoded)

    preview = ipc_org_repair(owner, {})
    assert preview["candidates"][0]["reason"] == "missing-directory-entry"
    ipc_org_repair(owner, {"apply": True})

    repaired_grants, _version = facade.read_text(acl.space_id, ACL_POLICY_DOC)
    assert repaired_grants == encoded


def test_timed_out_create_is_visible_as_creating_and_retry_reuses_spaces(
    apps, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_list

    owner, _bob = apps
    facade = owner._runtime.facade
    original_create = facade.create_space
    acl_create_entered = Event()
    release_create = Event()

    def delayed_create(name: str):
        if name == "group-acme-acl":
            acl_create_entered.set()
            assert release_create.wait(2)
        return original_create(name)

    monkeypatch.setattr(facade, "create_space", delayed_create)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ipc_org_create, owner, {"org": "group-acme"})
        assert acl_create_entered.wait(2)
        visible = ipc_org_list(owner, {})["orgs"]
        network = ipc_org_network(owner, {})
        retry = pool.submit(ipc_org_create, owner, {"org": "group-acme"})
        release_create.set()
        first_result = first.result(timeout=2)
        retry_result = retry.result(timeout=2)

    assert visible[0]["state"] == "creating"
    assert network["unavailableOrgs"] == [
        {"org": "group-acme", "available": False, "reason": "creating"}
    ]
    assert first_result["spaceId"] == retry_result["spaceId"]
    assert [space.name for space in facade.spaces()].count("group-acme") == 1
    assert [space.name for space in facade.spaces()].count("group-acme-acl") == 1


def _bound_legacy_org(owner: _App, org: str) -> tuple[str, str]:
    facade = owner._runtime.facade
    directory = facade.create_space(org)
    acl = facade.create_space(f"{org}-acl")
    facade.update_space_meta(directory.space_id, {"aclSpaceId": acl.space_id})
    facade.mkdir(acl.space_id, "policy")
    facade.write_text(
        acl.space_id,
        ACL_POLICY_DOC,
        json.dumps(default_role_grants_document(), sort_keys=True),
    )
    return directory.space_id, acl.space_id


def test_unmarked_org_state_distinguishes_partial_from_legacy_active(apps) -> None:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_list

    owner, _bob = apps
    facade = owner._runtime.facade
    partial = facade.create_space("group-partial")
    legacy = facade.create_space("group-legacy")
    acl = facade.create_space("group-legacy-acl")
    facade.update_space_meta(legacy.space_id, {"aclSpaceId": acl.space_id})
    facade.mkdir(acl.space_id, "policy")
    facade.write_text(
        acl.space_id,
        ACL_POLICY_DOC,
        json.dumps(default_role_grants_document(), sort_keys=True),
    )

    listed = {row["org"]: row["state"] for row in ipc_org_list(owner, {})["orgs"]}
    network = ipc_org_network(owner, {})

    assert listed == {
        "group-legacy": "legacy-active",
        "group-partial": "partial",
    }
    assert network["unavailableOrgs"] == [
        {"org": "group-partial", "available": False, "reason": "partial"}
    ]
    assert network["directory"]["group-partial"] == []
    assert partial.space_id != legacy.space_id


def test_marker_without_state_uses_bound_legacy_classification(apps) -> None:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_list

    owner, _bob = apps
    directory_id, _acl_id = _bound_legacy_org(owner, "group-legacy")
    owner._runtime.facade.mkdir(directory_id, "directory")
    owner._runtime.facade.write_text(
        directory_id, ORG_META_DOC, json.dumps({"aclSpaceId": "ignored"})
    )

    listed = ipc_org_list(owner, {})["orgs"]
    network = ipc_org_network(owner, {"org": "group-legacy"})

    assert listed[0]["state"] == "legacy-active"
    assert network["unavailableOrgs"] == []
    assert network["dataProblems"] == [
        {
            "org": "group-legacy",
            "path": ORG_META_DOC,
            "reason": "missing-state",
        }
    ]


def test_lifecycle_data_problem_reasons_match_contract_fixture(apps) -> None:
    owner, _bob = apps
    markers = {
        "invalid-json": "{broken",
        "invalid-object": "[]",
        "invalid-state": json.dumps({"state": ""}),
        "missing-state": "{}",
        "unknown-state": json.dumps({"state": "paused"}),
    }
    for reason, marker in markers.items():
        directory_id, _acl_id = _bound_legacy_org(owner, f"group-{reason}")
        owner._runtime.facade.mkdir(directory_id, "directory")
        owner._runtime.facade.write_text(directory_id, ORG_META_DOC, marker)

    contract_path = (
        Path(__file__).parents[4]
        / "contract"
        / "org-network"
        / "data-problem-reasons.json"
    )
    contracted = json.loads(contract_path.read_text(encoding="utf-8"))
    network = ipc_org_network(owner, {})
    actual = {
        problem["reason"]
        for problem in network["dataProblems"]
        if problem["path"] == ORG_META_DOC
    }

    assert actual == set(contracted["lifecycleMarker"]) == set(markers)


def test_corrupt_marker_uses_bound_legacy_classification_and_reports_problem(
    apps,
) -> None:
    from hyprial.daemon.impl.org.network.ipc import ipc_org_list

    owner, _bob = apps
    directory_id, _acl_id = _bound_legacy_org(owner, "group-legacy")
    owner._runtime.facade.mkdir(directory_id, "directory")
    owner._runtime.facade.write_text(directory_id, ORG_META_DOC, "{broken")

    listed = ipc_org_list(owner, {})["orgs"]
    network = ipc_org_network(owner, {})

    assert listed[0]["state"] == "legacy-active"
    assert network["unavailableOrgs"] == []
    assert network["dataProblems"] == [
        {
            "org": "group-legacy",
            "path": ORG_META_DOC,
            "reason": "invalid-json",
        }
    ]
    assert any(
        level == "warn"
        and event == "org.state.data-problem"
        and fields.get("reason") == "invalid-json"
        for level, event, fields in owner.events
    )


@pytest.mark.parametrize("state", ["creating", "partial"])
def test_explicit_incomplete_marker_is_refused(apps, state: str) -> None:
    owner, _bob = apps
    directory_id, _acl_id = _bound_legacy_org(owner, "group-acme")
    owner._runtime.facade.mkdir(directory_id, "directory")
    owner._runtime.facade.write_text(
        directory_id, ORG_META_DOC, json.dumps({"state": state})
    )

    aggregate = ipc_org_network(owner, {})
    with pytest.raises(DaemonRequestError) as explicit:
        ipc_org_network(owner, {"org": "group-acme"})

    assert aggregate["unavailableOrgs"] == [
        {"org": "group-acme", "available": False, "reason": state}
    ]
    assert aggregate["dataProblems"] == []
    assert explicit.value.code == "ORG_ACTION_NOT_AUTHORIZED"


def test_unknown_marker_state_is_refused_and_reports_value(apps) -> None:
    owner, _bob = apps
    directory_id, _acl_id = _bound_legacy_org(owner, "group-acme")
    owner._runtime.facade.mkdir(directory_id, "directory")
    owner._runtime.facade.write_text(
        directory_id, ORG_META_DOC, json.dumps({"state": "paused"})
    )

    network = ipc_org_network(owner, {})
    with pytest.raises(DaemonRequestError) as explicit:
        ipc_org_network(owner, {"org": "group-acme"})

    assert network["unavailableOrgs"] == [
        {"org": "group-acme", "available": False, "reason": "partial"}
    ]
    assert network["dataProblems"] == [
        {
            "org": "group-acme",
            "path": ORG_META_DOC,
            "reason": "unknown-state",
            "value": "paused",
        }
    ]
    assert explicit.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    assert any(
        level == "warn"
        and event == "org.state.data-problem"
        and fields.get("value") == "paused"
        for level, event, fields in owner.events
    )


def test_repair_does_not_apply_another_nodes_intent(
    apps, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.org.network import service
    from hyprial.daemon.impl.org.network.ipc import ipc_org_repair

    owner, _bob = apps
    original_update = service.update_org_create

    def interrupted_update(home, org, **values):
        if values.get("directory_space_id") is not None:
            raise RuntimeError("crash after directory space creation")
        return original_update(home, org, **values)

    monkeypatch.setattr(service, "update_org_create", interrupted_update)
    with pytest.raises(RuntimeError, match="crash after directory"):
        ipc_org_create(owner, {"org": "group-acme"})
    monkeypatch.setattr(service, "update_org_create", original_update)
    owner.node_id = "node-b"

    preview = ipc_org_repair(owner, {})
    assert preview["candidates"][0]["repairable"] is False
    assert ipc_org_repair(owner, {"apply": True})["repaired"] == []


def test_non_creator_cannot_resume_interrupted_create(
    apps, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, bob = apps
    facade = owner._runtime.facade
    original_create_space = facade.create_space

    def interrupted_create_space(name: str):
        if name == "group-acme-acl":
            raise RuntimeError("crash after directory space creation")
        return original_create_space(name)

    monkeypatch.setattr(facade, "create_space", interrupted_create_space)
    with pytest.raises(RuntimeError, match="crash after directory"):
        ipc_org_create(owner, {"org": "group-acme"})
    monkeypatch.setattr(facade, "create_space", original_create_space)

    with pytest.raises(DaemonRequestError) as denied:
        ipc_org_create(bob, {"org": "group-acme"})
    assert denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    assert [space.name for space in facade.spaces()].count("group-acme") == 1
    assert not any(space.name == "group-acme-acl" for space in facade.spaces())


def test_org_repair_is_dry_run_by_default_and_repairs_when_applied(apps) -> None:
    owner, _bob = apps
    facade = owner._runtime.facade
    directory = facade.create_space("group-acme")
    repair = getattr(__import__(
        "hyprial.daemon.impl.org.network.ipc", fromlist=["ipc_org_repair"]
    ), "ipc_org_repair")

    preview = repair(owner, {})
    assert preview == {
        "apply": False,
        "candidates": [
            {
                "org": "group-acme",
                "spaceId": directory.space_id,
                "reason": "missing-acl-binding",
                "repairable": True,
            }
        ],
        "repaired": [],
    }
    assert [space.name for space in facade.spaces()] == ["group-acme"]

    applied = repair(owner, {"apply": True})
    assert [row["org"] for row in applied["repaired"]] == ["group-acme"]
    assert [space.name for space in facade.spaces()].count("group-acme") == 1
    assert [space.name for space in facade.spaces()].count("group-acme-acl") == 1


def test_create_binds_acl_by_id_and_ignores_foreign_name_squats(apps) -> None:
    owner, _bob = apps
    state = owner.hyprial_home / "state"
    owner._runtime.close()

    foreign_runtime = OrgFsRuntime(
        state, node_id="node-b", author=f"user:{BOB}"
    )
    preexisting = foreign_runtime.facade.create_space("group-acme-acl")
    foreign_runtime.facade.mkdir(preexisting.space_id, "policy")
    document = default_role_grants_document()
    grants = document["grants"]
    assert isinstance(grants, list)
    grants.append(
        {"role": "member", "resource": "org:members", "action": "remove"}
    )
    foreign_runtime.facade.write_text(
        preexisting.space_id,
        ACL_POLICY_DOC,
        json.dumps(document, sort_keys=True, separators=(",", ":")),
    )
    foreign_runtime.close()

    reopened = OrgFsRuntime(
        state, node_id="node-a", author=f"user:{OWNER}"
    )
    try:
        owner = _App(reopened, owner.hyprial_home)
        created = ipc_org_create(owner, {"org": "group-acme"})
        assert created["aclSpaceId"] != preexisting.space_id
        assert (
            reopened.facade.space_meta(created["spaceId"])["aclSpaceId"]
            == created["aclSpaceId"]
        )
        lifecycle, _version = reopened.facade.read_text(
            created["spaceId"], ORG_META_DOC
        )
        assert "aclSpaceId" not in json.loads(lifecycle)

        ipc_org_invite(owner, {"org": "group-acme", "user": f"user:{BOB}"})
        ipc_org_invite(owner, {"org": "group-acme", "user": f"user:{CAROL}"})
        post_restart = reopened.facade.create_space("group-acme-acl")
        reopened.facade.mkdir(post_restart.space_id, "policy")
        reopened.facade.write_text(
            post_restart.space_id,
            ACL_POLICY_DOC,
            json.dumps(document, sort_keys=True, separators=(",", ":")),
        )
    finally:
        reopened.close()

    after_restart = OrgFsRuntime(
        state, node_id="node-a", author=f"user:{OWNER}"
    )
    try:
        bob_after_restart = _App(after_restart, owner.hyprial_home, BOB)
        with pytest.raises(DaemonRequestError) as denied:
            ipc_org_remove(
                bob_after_restart,
                {"org": "group-acme", "user": f"user:{CAROL}"},
            )
        assert denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    finally:
        after_restart.close()


def test_member_written_directory_document_binding_is_ignored(apps) -> None:
    owner, _bob = apps
    created = ipc_org_create(owner, {"org": "group-acme"})
    ipc_org_invite(owner, {"org": "group-acme", "user": f"user:{BOB}"})
    ipc_org_invite(owner, {"org": "group-acme", "user": f"user:{CAROL}"})

    decoy = owner._runtime.facade.create_space("member-policy-decoy")
    owner._runtime.facade.mkdir(decoy.space_id, "policy")
    document = default_role_grants_document()
    grants = document["grants"]
    assert isinstance(grants, list)
    grants.append(
        {"role": "member", "resource": "org:members", "action": "remove"}
    )
    owner._runtime.facade.write_text(
        decoy.space_id,
        ACL_POLICY_DOC,
        json.dumps(document, sort_keys=True, separators=(",", ":")),
    )

    state = owner.hyprial_home / "state"
    owner._runtime.close()
    member_runtime = OrgFsRuntime(
        state, node_id="node-b", author=f"user:{BOB}"
    )
    # Org meta is owner-only on every replica, so the member's rebinding is
    # refused at write time; the reader below must still ignore it.
    try:
        with pytest.raises(OrgFsError) as refused:
            member_runtime.facade.write_text(
                created["spaceId"],
                ORG_META_DOC,
                json.dumps(
                    {"state": "active", "aclSpaceId": decoy.space_id},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        assert refused.value.code == "not-a-member"
    finally:
        member_runtime.close()

    reopened = OrgFsRuntime(
        state, node_id="node-a", author=f"user:{OWNER}"
    )
    try:
        assert (
            reopened.facade.space_meta(created["spaceId"])["aclSpaceId"]
            == created["aclSpaceId"]
        )
        bob = _App(reopened, owner.hyprial_home, BOB)
        with pytest.raises(DaemonRequestError) as denied:
            ipc_org_remove(bob, {"org": "group-acme", "user": f"user:{CAROL}"})
        assert denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    finally:
        reopened.close()


def test_acl_binding_owner_mismatch_denies_through_org_ipc(apps) -> None:
    owner, _bob = apps
    created = ipc_org_create(owner, {"org": "group-acme"})
    state = owner.hyprial_home / "state"
    owner._runtime.close()

    foreign_runtime = OrgFsRuntime(
        state, node_id="node-b", author=f"user:{BOB}"
    )
    foreign = foreign_runtime.facade.create_space("unrelated-policy")
    foreign_runtime.close()

    reopened = OrgFsRuntime(
        state, node_id="node-a", author=f"user:{OWNER}"
    )
    try:
        reopened.facade.update_space_meta(
            created["spaceId"], {"aclSpaceId": foreign.space_id}
        )
        app = _App(reopened, owner.hyprial_home)
        with pytest.raises(DaemonRequestError) as denied:
            ipc_org_network(app, {"org": "group-acme"})
        assert denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"
        assert any(
            event == "org.policy.grants-unavailable"
            and fields.get("reason") == "acl-owner-mismatch"
            for _level, event, fields in app.events
        )
    finally:
        reopened.close()


def test_ambiguous_directory_name_denies_policy_through_org_ipc(apps) -> None:
    owner, _bob = apps
    ipc_org_create(owner, {"org": "group-acme"})
    owner._runtime.facade.create_space("group-acme")

    with pytest.raises(DaemonRequestError) as denied:
        ipc_org_delete(owner, {"org": "group-acme"})
    assert denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    assert any(
        event == "org.policy.grants-unavailable"
        and fields.get("reason") == "ambiguous-directory-space"
        for _level, event, fields in owner.events
    )


def test_missing_acl_binding_denies_default_grants_through_org_ipc(apps) -> None:
    owner, bob = apps
    directory = owner._runtime.facade.create_space("group-acme")
    acl = owner._runtime.facade.create_space("group-acme-acl")
    owner._runtime.facade.invite(directory.space_id, f"user:{BOB}", "rw")
    owner._runtime.facade.invite(acl.space_id, f"user:{BOB}", "ro")

    with pytest.raises(DaemonRequestError) as denied:
        ipc_org_leave(bob, {"org": "group-acme"})
    assert denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    assert any(
        event == "org.policy.grants-unavailable"
        and fields.get("reason") == "missing-acl-binding"
        for _level, event, fields in bob.events
    )


def test_unknown_acl_binding_id_denies_through_org_ipc(apps) -> None:
    owner, _bob = apps
    created = ipc_org_create(owner, {"org": "group-acme"})
    owner._runtime.facade.update_space_meta(
        created["spaceId"], {"aclSpaceId": "unknown-space-id"}
    )

    with pytest.raises(DaemonRequestError) as denied:
        ipc_org_network(owner, {"org": "group-acme"})
    assert denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    assert any(
        event == "org.policy.grants-unavailable"
        and fields.get("reason") == "unknown-acl-space-id"
        for _level, event, fields in owner.events
    )


def test_network_keeps_healthy_org_when_an_unbound_org_is_unavailable(apps) -> None:
    owner, _bob = apps
    ipc_org_create(owner, {"org": "group-acme"})
    owner._runtime.facade.create_space("group-junk")

    result = ipc_org_network(owner, {})

    assert result["orgs"] == ["group-acme", "group-junk"]
    assert [row["deviceId"] for row in result["directory"]["group-acme"]] == [
        "node-a"
    ]
    assert result["directory"]["group-junk"] == []
    assert result["unavailableOrgs"] == [
        {"org": "group-junk", "available": False, "reason": "partial"}
    ]
    assert not any(
        event == "org.policy.grants-unavailable"
        and fields.get("org") == "group-junk"
        for _level, event, fields in owner.events
    )

    with pytest.raises(DaemonRequestError) as explicit:
        ipc_org_network(owner, {"org": "group-junk"})
    assert explicit.value.code == "ORG_ACTION_NOT_AUTHORIZED"


def test_owner_cannot_delete_an_unbound_org_via_fallback(apps) -> None:
    owner, _bob = apps
    space = owner._runtime.facade.create_space("group-junk")
    before_meta = owner._runtime.facade.space_meta(space.space_id)
    before_nodes = deepcopy(owner._runtime.facade._space(space.space_id).nodes)
    before_members = _member_modes(owner, "group-junk")

    with pytest.raises(DaemonRequestError) as denied:
        ipc_org_delete(owner, {"org": "group-junk"})
    assert denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    assert not any(event.endswith("org.delete.unbound-owner-fallback")
                   for _level, event, _fields in owner.events)
    assert _space_id(owner, "group-junk") == space.space_id
    assert owner._runtime.facade.space_meta(space.space_id) == before_meta
    assert owner._runtime.facade._space(space.space_id).nodes == before_nodes
    assert _member_modes(owner, "group-junk") == before_members


def test_owner_cannot_delete_an_unbound_org_that_still_has_members(apps) -> None:
    # Missing policy cannot authorize a destructive operation or partial deletion.
    owner, _bob = apps
    space = owner._runtime.facade.create_space("group-legacy")
    owner._runtime.facade.invite(space.space_id, "user:carol", "rw")
    before_meta = owner._runtime.facade.space_meta(space.space_id)
    before_nodes = deepcopy(owner._runtime.facade._space(space.space_id).nodes)

    with pytest.raises(DaemonRequestError) as denied:
        ipc_org_delete(owner, {"org": "group-legacy"})
    assert denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    assert {member.user for member in owner._runtime.facade.members(space.space_id)} == {
        space.owner, "user:carol",
    }
    assert owner._runtime.facade.space_meta(space.space_id) == before_meta
    assert owner._runtime.facade._space(space.space_id).nodes == before_nodes


def test_member_cannot_delete_an_unbound_org(apps) -> None:
    owner, member = apps
    owner._runtime.facade.create_space("group-junk")

    with pytest.raises(DaemonRequestError) as denied:
        ipc_org_delete(member, {"org": "group-junk"})
    assert denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"


def test_valid_acl_binding_keeps_delete_policy_authoritative(apps) -> None:
    owner, _bob = apps
    created = ipc_org_create(owner, {"org": "group-acme"})
    document = default_role_grants_document()
    grants = document["grants"]
    assert isinstance(grants, list)
    document["grants"] = [grant for grant in grants if grant.get("role") != "owner"]
    owner._runtime.facade.write_text(
        created["aclSpaceId"],
        ACL_POLICY_DOC,
        json.dumps(document, sort_keys=True, separators=(",", ":")),
    )

    with pytest.raises(DaemonRequestError) as denied:
        ipc_org_delete(owner, {"org": "group-acme"})
    assert denied.value.code == "ORG_ACTION_NOT_AUTHORIZED"
    assert not any(
        event.endswith("org.delete.unbound-owner-fallback")
        for _level, event, _fields in owner.events
    )
