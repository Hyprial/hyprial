"""OC7 lifecycle through the daemon IPC entry points and the method registry.

D5 (contract OC0 §3.9): a legacy org whose org cell was never created must
not be backfilled by ``org.repair --apply``, a repeated ``org.create``,
``org.invite`` or a roster read. The named lifecycle errors must reach the
wire with their own codes rather than being rewritten to ``DAEMON_ERROR`` by
the registry's declared-error check. The actor path must also reach an org
whose OrgFS marker is deleted while its tailnet is still open.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.composition.cells import cell_runtime
from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
from hyprial.daemon.impl.org.cell_actor import org_cell_actors
from hyprial.daemon.impl.org.network.ipc import (
    _build_service,
    ipc_org_create,
    ipc_org_delete,
    ipc_org_invite,
    ipc_org_network,
    ipc_org_repair,
)
from hyprial.daemon.impl.org.network.state import begin_org_create
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.request_catalog.registry import _build_registry
from hyprial.identity import ORG_META_DOC
from hyprial.kernel import DaemonRequestError, ipc_errors, keyed_cell_id

from tests.daemon.org.cell_actor.test_org_cell_actor import (
    ALICE_DEVICE_UID,
    ALICE_OWNER,
    ALICE_UID,
    BOB_OWNER,
    _OrgApp,
)

ORG = "group-acme"


@pytest.fixture
def app(tmp_path: Path):
    app = _OrgApp(
        tmp_path,
        owner=ALICE_OWNER,
        user_id=ALICE_UID,
        node_id="node-a",
        device_uid=ALICE_DEVICE_UID,
    )
    yield app
    app.close()


def _org_cell(app: _OrgApp, space_id: str):
    runtime = cell_runtime(app)
    assert runtime is not None
    return runtime.lookup(keyed_cell_id("org", runtime.identity.authority, space_id))


def _legacy_org(app: _OrgApp) -> str:
    service = _build_service(app)
    service._ensure_owner_cell = None
    return service.create(ORG)["spaceId"]


def _wire_code(app: _OrgApp, method: str, params: dict) -> str:
    with pytest.raises(DaemonRequestError) as raised:
        _build_registry().invoke(app, method, params)
    return raised.value.code


def test_never_created_org_cell_is_not_backfilled(app: _OrgApp) -> None:
    space_id = _legacy_org(app)
    assert _org_cell(app, space_id) is None

    view = ipc_org_network(app, {})
    assert any(
        problem.get("org") == ORG and problem.get("reason") == "org-cell-unbound"
        for problem in view["dataProblems"]
    )
    ipc_org_repair(app, {"org": ORG, "apply": True})
    assert _org_cell(app, space_id) is None
    ipc_org_create(app, {"org": ORG})
    assert _org_cell(app, space_id) is None
    org_cell_actors(app).for_org(ORG).roster()
    assert _org_cell(app, space_id) is None
    with pytest.raises(DaemonRequestError) as refused:
        ipc_org_invite(app, {"org": ORG, "user": f"user:{BOB_OWNER}"})
    assert refused.value.code == "ORG_CELL_UNBOUND"
    assert _org_cell(app, space_id) is None


def test_unbound_error_keeps_its_code_on_the_wire(app: _OrgApp) -> None:
    _legacy_org(app)

    assert (
        _wire_code(app, "org.invite", {"org": ORG, "user": f"user:{BOB_OWNER}"})
        == "ORG_CELL_UNBOUND"
    )


def test_deletion_errors_keep_their_codes_on_the_wire(app: _OrgApp) -> None:
    ipc_org_create(app, {"org": ORG})
    registry = _build_registry()
    preview = registry.invoke(app, "org.delete", {"org": ORG})

    assert (
        _wire_code(
            app,
            "org.delete",
            {"org": ORG, "confirmed": True, "operationId": "not-the-preview"},
        )
        == "REMOVAL_CONFIRMATION_STALE"
    )
    done = registry.invoke(
        app,
        "org.delete",
        {"org": ORG, "confirmed": True, "operationId": preview["operationId"]},
    )
    assert done["phase"] == "complete"
    assert _wire_code(app, "org.create", {"org": ORG}) == "ORG_ALREADY_DELETED"


def test_owner_leave_with_marker_deleted_and_tailnet_open_is_refused(
    app: _OrgApp,
) -> None:
    created = ipc_org_create(app, {"org": ORG})
    _build_service(app)._store.put_org_meta(ORG, {"state": "deleted"})

    assert (
        _wire_code(app, "org.leave", {"org": ORG, "confirmed": True})
        == "LAST_AUTHORITY_REFUSED"
    )
    tailnet = app._forwarding_supervisor.tailnets[created["spaceId"]]
    assert not tailnet.get("closed")


def test_authority_finishes_a_marker_deleted_org_through_the_actor(
    app: _OrgApp,
) -> None:
    created = ipc_org_create(app, {"org": ORG})
    _build_service(app)._store.put_org_meta(ORG, {"state": "deleted"})

    preview = ipc_org_delete(app, {"org": ORG})
    assert preview["phase"] == "pending-confirmation"
    done = ipc_org_delete(
        app, {"org": ORG, "confirmed": True, "operationId": preview["operationId"]}
    )

    assert done["phase"] == "complete"
    assert app._forwarding_supervisor.tailnets[created["spaceId"]]["closed"] is True


def test_repair_of_an_incomplete_legacy_org_creates_no_org_cell(app: _OrgApp) -> None:
    facade = app._runtime.facade
    directory = facade.create_space(ORG)
    acl = facade.create_space(f"{ORG}-acl")
    facade.update_space_meta(directory.space_id, {"aclSpaceId": acl.space_id})

    applied = ipc_org_repair(app, {"apply": True})

    assert applied["repaired"][0]["spaceId"] == directory.space_id
    assert _org_cell(app, directory.space_id) is None


def test_owner_leave_after_deletion_is_local_cleanup_on_the_wire(app: _OrgApp) -> None:
    created = ipc_org_create(app, {"org": ORG})
    registry = _build_registry()
    preview = registry.invoke(app, "org.delete", {"org": ORG})
    registry.invoke(
        app,
        "org.delete",
        {"org": ORG, "confirmed": True, "operationId": preview["operationId"]},
    )
    assert app._forwarding_supervisor.tailnets[created["spaceId"]]["closed"] is True

    left = registry.invoke(app, "org.leave", {"org": ORG, "confirmed": True})

    assert left["reason"] == "org-deleted"
    assert created["spaceId"] not in app._forwarding_supervisor.tailnets


def test_delete_without_a_supervisor_fails_closed_on_the_wire(app: _OrgApp) -> None:
    created = ipc_org_create(app, {"org": ORG})
    sidecar = app._forwarding_supervisor
    app._forwarding_supervisor = None

    assert _wire_code(app, "org.delete", {"org": ORG}) == "MEMBERSHIP_NOT_INITIALIZED"

    app._forwarding_supervisor = sidecar
    assert _build_service(app)._org_state(ORG) == "active"
    assert not sidecar.tailnets[created["spaceId"]].get("closed")
    assert _build_registry().invoke(app, "org.delete", {"org": ORG})["phase"] == (
        "pending-confirmation"
    )


def test_sidecar_error_details_do_not_become_daemon_error(app: _OrgApp) -> None:
    ipc_org_create(app, {"org": ORG})

    def timed_out(op: str, params: dict, *, timeout: float | None = None) -> dict:
        raise ForwardingSidecarError(
            "Tailnet membership command timed out.",
            code=ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT,
            details={"operation": op, "leg": op, "budgetSeconds": 1.0},
        )

    app._forwarding_supervisor.membership_request = timed_out

    assert (
        _wire_code(app, "org.delete", {"org": ORG})
        == ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT
    )


@pytest.mark.parametrize(
    "code",
    [
        "OPERATION_CONFLICT",
        "MEMBERSHIP_LIMIT_EXCEEDED",
        "BAD_REQUEST",
        "AUTHORITY_UNREACHABLE",
        "FORWARD_PROTOCOL",
        "SIDECAR_PROTOCOL_MISMATCH",
    ],
)
def test_reachable_sidecar_codes_are_declared_on_delete(
    app: _OrgApp, code: str
) -> None:
    ipc_org_create(app, {"org": ORG})
    app._forwarding_supervisor.failures["membership-status"] = code

    assert _wire_code(app, "org.delete", {"org": ORG}) == code


def _raise_timeout(app: _OrgApp) -> None:
    def overloaded(op: str, params: dict, *, timeout: float | None = None) -> dict:
        raise TimeoutError("forwarding supervisor state overloaded")

    app._forwarding_supervisor.membership_request = overloaded


def _raise_sidecar_timeout(app: _OrgApp) -> None:
    def timed_out(op: str, params: dict, *, timeout: float | None = None) -> dict:
        raise ForwardingSidecarError(
            "Tailnet membership command timed out.",
            code=ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT,
            details={"operation": op, "leg": op, "budgetSeconds": 1.0},
        )

    app._forwarding_supervisor.membership_request = timed_out


def _no_supervisor(app: _OrgApp) -> None:
    app._forwarding_supervisor = None


def _start_raises(app: _OrgApp) -> None:
    def broken() -> None:
        raise RuntimeError("supervisor start failed")

    app._start_forwarding_supervisor = broken


def _malformed_status(app: _OrgApp) -> None:
    def malformed(op: str, params: dict, *, timeout: float | None = None) -> dict:
        return {"tailnets": "not-a-list"}

    app._forwarding_supervisor.membership_request = malformed


@pytest.mark.parametrize(
    "break_sidecar",
    [_raise_timeout, _raise_sidecar_timeout, _no_supervisor, _start_raises, _malformed_status],
)
@pytest.mark.parametrize("params", [{}, {"org": ORG}])
def test_unavailable_tailnet_status_is_a_network_data_problem(
    app: _OrgApp, break_sidecar, params: dict
) -> None:
    ipc_org_create(app, {"org": ORG})
    break_sidecar(app)

    view = ipc_org_network(app, params)

    assert {
        "org": ORG,
        "path": "tailnet",
        "reason": "tailnet-status-unavailable",
    } in view["dataProblems"]
    assert not any(
        problem.get("reason") == "org-deleted" for problem in view["dataProblems"]
    )


def test_network_view_bounds_the_tailnet_status_probe_under_doctor(
    app: _OrgApp,
) -> None:
    ipc_org_create(app, {"org": ORG})
    sidecar = app._forwarding_supervisor
    sidecar.timeouts.clear()

    view = ipc_org_network(app, {"org": ORG})

    assert not any(
        problem.get("reason") == "tailnet-status-unavailable"
        for problem in view["dataProblems"]
    )
    probes = [timeout for op, timeout in sidecar.timeouts if op == "membership-status"]
    assert probes and all(
        timeout is not None and timeout < 2.0 for timeout in probes
    )


SUB = "group-dept"


def _sub_org(app: _OrgApp) -> dict:
    ipc_org_create(app, {"org": ORG})
    return ipc_org_create(app, {"org": SUB, "parent": ORG})


def _conflict(app: _OrgApp, params: dict) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as raised:
        _build_registry().invoke(app, "org.delete", params)
    assert raised.value.code == "OPERATION_CONFLICT"
    return raised.value


def test_parent_delete_is_refused_while_a_sub_org_is_live(app: _OrgApp) -> None:
    created = _sub_org(app)

    error = _conflict(app, {"org": ORG})

    assert SUB in str(error)
    service = _build_service(app)
    assert service._org_state(ORG) == "active"
    assert not app._forwarding_supervisor.tailnets[created["tailnetId"]].get("closed")
    assert not (app.hyprial_home / f"state/org/deletions/{ORG}.json").exists()


def test_parent_confirmation_is_refused_when_a_sub_org_appears_after_preview(
    app: _OrgApp,
) -> None:
    root = ipc_org_create(app, {"org": ORG})
    preview = _build_registry().invoke(app, "org.delete", {"org": ORG})
    ipc_org_create(app, {"org": SUB, "parent": ORG})

    error = _conflict(
        app, {"org": ORG, "confirmed": True, "operationId": preview["operationId"]}
    )

    assert SUB in str(error)
    assert _build_service(app)._org_state(ORG) == "active"
    assert not app._forwarding_supervisor.tailnets[root["spaceId"]].get("closed")


def test_sub_org_delete_keeps_the_inherited_tailnet_then_parent_deletes_leaf_first(
    app: _OrgApp,
) -> None:
    created = _sub_org(app)
    tailnet_id = created["tailnetId"]
    sidecar = app._forwarding_supervisor
    revision = sidecar.tailnets[tailnet_id]["revision"]
    registry = _build_registry()

    preview = registry.invoke(app, "org.delete", {"org": SUB})
    assert preview["tailnetPhase"] == "inherited"
    done = registry.invoke(
        app, "org.delete", {"org": SUB, "confirmed": True, "operationId": preview["operationId"]}
    )

    assert done["phase"] == "complete"
    assert done["tailnetClosed"] is False
    assert done["tailnetPhase"] == "inherited"
    assert not sidecar.tailnets[tailnet_id].get("closed")
    assert sidecar.tailnets[tailnet_id]["revision"] == revision
    assert not any(
        op in ("tailnet-shutdown", "tailnet-forget") and params.get("tailnetId") == tailnet_id
        for op, params in sidecar.calls
    )
    service = _build_service(app)
    assert service._org_state(SUB) == "deleted"
    assert service._org_state(ORG) == "active"
    assert _org_cell(app, created["spaceId"]).retired

    parent_preview = registry.invoke(app, "org.delete", {"org": ORG})
    parent_done = registry.invoke(
        app,
        "org.delete",
        {"org": ORG, "confirmed": True, "operationId": parent_preview["operationId"]},
    )
    assert parent_done["phase"] == "complete"
    assert sidecar.tailnets[tailnet_id]["closed"] is True


def test_sub_org_delete_revokes_its_members_but_not_the_parent_membership(
    app: _OrgApp,
) -> None:
    created = _sub_org(app)
    facade = app._runtime.facade
    root_space = _build_service(app)._find_space(ORG).space_id
    facade.invite(root_space, "user:bob", "rw")
    facade.invite(created["spaceId"], "user:bob", "rw")
    registry = _build_registry()

    preview = registry.invoke(app, "org.delete", {"org": SUB})
    registry.invoke(
        app, "org.delete", {"org": SUB, "confirmed": True, "operationId": preview["operationId"]}
    )

    assert "user:bob" not in {row.user for row in facade.members(created["spaceId"])}
    assert "user:bob" in {row.user for row in facade.members(root_space)}


def test_completed_delete_retires_the_owner_org_cell(app: _OrgApp) -> None:
    created = ipc_org_create(app, {"org": ORG})
    assert _org_cell(app, created["spaceId"]) is not None
    registry = _build_registry()

    preview = registry.invoke(app, "org.delete", {"org": ORG})
    registry.invoke(
        app, "org.delete", {"org": ORG, "confirmed": True, "operationId": preview["operationId"]}
    )

    assert _org_cell(app, created["spaceId"]).retired


def test_finishing_a_marker_deleted_org_retires_the_owner_org_cell(app: _OrgApp) -> None:
    created = ipc_org_create(app, {"org": ORG})
    _build_service(app)._store.put_org_meta(ORG, {"state": "deleted"})

    preview = ipc_org_delete(app, {"org": ORG})
    ipc_org_delete(
        app, {"org": ORG, "confirmed": True, "operationId": preview["operationId"]}
    )

    assert _org_cell(app, created["spaceId"]).retired


def _write_meta(app: _OrgApp, space_id: str, meta: dict) -> None:
    app._runtime.facade.write_text(space_id, ORG_META_DOC, json.dumps(meta))


def _fail_meta_reads(app: _OrgApp, space_id: str, monkeypatch) -> None:
    facade = app._runtime.facade
    read_text = facade.read_text

    def failing(space: str, node):
        if space == space_id and node == ORG_META_DOC:
            raise OrgFsError("io-error", {"message": "simulated"})
        return read_text(space, node)

    monkeypatch.setattr(facade, "read_text", failing)


def _complete_delete(app: _OrgApp, org: str) -> dict:
    registry = _build_registry()
    preview = registry.invoke(app, "org.delete", {"org": org})
    return registry.invoke(
        app, "org.delete", {"org": org, "confirmed": True, "operationId": preview["operationId"]}
    )


def test_parent_delete_fails_closed_on_unreadable_org_metadata(
    app: _OrgApp, monkeypatch
) -> None:
    created = _sub_org(app)
    _fail_meta_reads(app, created["spaceId"], monkeypatch)

    error = _conflict(app, {"org": ORG})

    assert SUB in str(error)
    assert not app._forwarding_supervisor.tailnets[created["tailnetId"]].get("closed")


def test_parent_delete_consults_the_sub_org_cell_parent(app: _OrgApp) -> None:
    created = _sub_org(app)
    _write_meta(app, created["spaceId"], {"state": "active"})

    error = _conflict(app, {"org": ORG})

    assert SUB in str(error)


def test_parent_delete_counts_an_interrupted_sub_org_create(app: _OrgApp) -> None:
    root = ipc_org_create(app, {"org": ORG})
    begin_org_create(
        app.hyprial_home,
        org=SUB,
        node_id="node-a",
        owner=ALICE_OWNER,
        started_at="2026-11-23T00:00:00+00:00",
        parent=ORG,
        parent_space_id=root["spaceId"],
        tailnet_id=root["spaceId"],
    )

    error = _conflict(app, {"org": ORG})

    assert SUB in str(error)


def test_sub_org_without_a_recorded_tailnet_still_inherits(app: _OrgApp) -> None:
    created = _sub_org(app)
    _write_meta(app, created["spaceId"], {"state": "active", "parent": ORG})

    done = _complete_delete(app, SUB)

    assert done["tailnetPhase"] == "inherited"
    assert not app._forwarding_supervisor.tailnets[created["tailnetId"]].get("closed")


def test_sub_org_delete_revokes_acl_only_members(app: _OrgApp) -> None:
    _sub_org(app)
    facade = app._runtime.facade
    acl_space = _build_service(app)._require_acl_space(SUB).space_id
    facade.invite(acl_space, "user:carol", "rw")

    _complete_delete(app, SUB)

    assert "user:carol" not in {row.user for row in facade.members(acl_space)}


def test_marker_deleted_sub_org_without_a_journal_is_finished(app: _OrgApp) -> None:
    created = _sub_org(app)
    facade = app._runtime.facade
    facade.invite(created["spaceId"], "user:bob", "rw")
    _build_service(app)._store.put_org_meta(SUB, {"state": "deleted"})

    done = _complete_delete(app, SUB)

    assert done["phase"] == "complete"
    assert "user:bob" not in {row.user for row in facade.members(created["spaceId"])}
    assert _org_cell(app, created["spaceId"]).retired
    assert not app._forwarding_supervisor.tailnets[created["tailnetId"]].get("closed")


def test_sub_org_delete_requires_the_inherited_tailnet_row(app: _OrgApp) -> None:
    created = _sub_org(app)
    table = app._forwarding_supervisor.tailnets.pop(created["tailnetId"])

    assert _wire_code(app, "org.delete", {"org": SUB}) == "AUTHORITY_REQUIRED"

    app._forwarding_supervisor.tailnets[created["tailnetId"]] = table
    assert _build_service(app)._org_state(SUB) == "active"


def test_deleted_sub_org_cleanup_never_touches_a_tailnet(app: _OrgApp) -> None:
    _sub_org(app)
    removal_dir = app.hyprial_home / "state" / "sub-org-removal"
    removal_dir.mkdir(parents=True, exist_ok=True)
    (removal_dir / f"{SUB}--bob.json").write_text("{}", encoding="utf-8")
    _complete_delete(app, SUB)
    sidecar = app._forwarding_supervisor
    sidecar.calls.clear()

    left = _build_registry().invoke(app, "org.leave", {"org": SUB, "confirmed": True})

    assert left["reason"] == "org-deleted"
    assert not any(op == "tailnet-forget" for op, _params in sidecar.calls)
    assert not (removal_dir / f"{SUB}--bob.json").exists()
