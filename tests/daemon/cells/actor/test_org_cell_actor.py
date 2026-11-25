"""OC2: ``org`` cells in CellActorCore, CellActorRegistry and cell actor wiring."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hyprial.daemon.impl.cells.actor import (
    CellActorRegistry,
    CellActorState,
    headless_admission,
)
from hyprial.daemon.impl.cells.actor.actor import CellActorCore, mint_epoch
from hyprial.daemon.impl.cells.model import (
    Cell,
    CellBinding,
    CellMember,
    CellPermissions,
    CellScope,
)
from hyprial.daemon.impl.cells.ports import CellSpec
from hyprial.daemon.impl.composition.actors import ActorHostRegistry
from hyprial.daemon.impl.composition.cells import cell_runtime, close_cell_runtime
from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.identity import org_space_name
from hyprial.kernel import keyed_cell_id
from tests.daemon.cells.actor.actor_support import Clock, host as actor_host, wait_until
from tests.daemon.cells.actor.test_registry import Runtime
from tests.daemon.cells.runtime.cell_runtime_support import (
    ALICE,
    ORG,
    authority_for,
    uid_for,
)

BOB = authority_for("user:bob")
ALICE_REF = f"u.{uid_for('user:alice')}"
BOB_REF = f"u.{uid_for('user:bob')}"
DEVICE_A = "d." + "a" * 32
DEVICE_B = "d." + "b" * 32
SPACE_ID = "sp-acme-1"


def _org_cell(
    *,
    authority: str = ALICE,
    org_name: str = ORG,
    space_id: str = SPACE_ID,
    revision: int = 1,
    retired: bool = False,
    updated_at_ms: int = 1_000,
    with_bob: bool = False,
) -> Cell:
    owner_ref = authority.removeprefix("user:")
    owner_name = "alice@example.com" if authority == ALICE else "bob@example.com"
    owner_device = DEVICE_A if authority == ALICE else DEVICE_B
    members = [CellMember(owner_ref, "owner")]
    org_members: dict[str, Any] = {
        owner_ref: {
            "username": owner_name,
            "joinedAtMs": 1_000,
            "invitedBy": owner_ref,
            "devices": [
                {
                    "deviceUid": owner_device,
                    "keyId": None,
                    "publicKey": None,
                    "verifiedAtMs": 1_000,
                }
            ],
        }
    }
    if with_bob and authority == ALICE:
        members.append(CellMember(BOB_REF, "member"))
        org_members[BOB_REF] = {
            "username": "bob@example.com",
            "joinedAtMs": 1_500,
            "invitedBy": ALICE_REF,
            "devices": [
                {
                    "deviceUid": DEVICE_B,
                    "keyId": None,
                    "publicKey": None,
                    "verifiedAtMs": 1_500,
                }
            ],
        }
    return Cell(
        cell_id=keyed_cell_id("org", authority, space_id),
        kind="org",
        authority=authority,
        scope=CellScope.of_org(org_name),
        binding=CellBinding.keyed(space_id),
        name=org_name,
        members=tuple(members),
        permissions=CellPermissions(),
        params={
            "org.name": org_name,
            "org.issuer": "https://login.example.com",
            "org.directorySpace": space_id,
            "org.tailnet": space_id,
            "org.parent": None,
            "org.members": org_members,
        },
        revision=revision,
        retired=retired,
        updated_at_ms=updated_at_ms,
    )


def test_org_cell_core_projects_active_without_headless_entry() -> None:
    cell = _org_cell(with_bob=True)
    core = CellActorCore(cell_id=cell.cell_id, epoch=mint_epoch(), clock_ms=Clock())
    assert core.apply_document(cell)
    snapshot = core.snapshot()
    assert snapshot.state is CellActorState.ACTIVE
    assert snapshot.reason is None
    assert snapshot.entry_member is None
    assert headless_admission(snapshot).admitted is False
    assert {(m.name, m.role) for m in snapshot.members} == {
        (ALICE_REF, "owner"),
        (BOB_REF, "member"),
    }

    conflict = _org_cell(with_bob=True, revision=1, updated_at_ms=2_000)
    assert core.apply_document(conflict)
    assert core.snapshot().state is CellActorState.DEGRADED
    assert core.snapshot().reason == "revision-conflict"

    retired = _org_cell(with_bob=True, revision=2, retired=True)
    assert core.apply_document(retired)
    assert core.snapshot().state is CellActorState.RETIRED


def test_org_cell_registry_publishes_active_snapshot() -> None:
    cell = _org_cell()
    engine = actor_host()
    runtime = Runtime(cell)
    registry = CellActorRegistry(engine, runtime=runtime)
    try:
        registry.on_index({cell.cell_id})
        wait_until(lambda: registry.snapshot(cell.cell_id) is not None)
        snapshot = registry.snapshot(cell.cell_id)
        assert snapshot is not None
        assert snapshot.state is CellActorState.ACTIVE
        assert snapshot.entry_member is None
        assert headless_admission(snapshot).admitted is False
    finally:
        registry.detach()
        assert engine.shutdown()


def test_composition_wiring_attaches_only_local_authority_org_cells(tmp_path: Path) -> None:
    fs = LocalOrgFs(author="user:alice")
    space = fs.create_space(org_space_name(ORG))
    app = SimpleNamespace(
        state_dir=tmp_path / "state",
        hyprial_home=tmp_path / "home",
        owner="alice",
        user_id=uid_for("user:alice"),
        user_issuer="https://login.example.com",
        _require_orgfs_runtime=lambda: SimpleNamespace(facade=fs),
        _actor_hosts=ActorHostRegistry(),
        agents=SimpleNamespace(list=lambda: ()),
        _log=lambda *_args, **_kwargs: None,
    )
    try:
        runtime = cell_runtime(app)
        assert runtime is not None
        owned = _org_cell(authority=ALICE, space_id=space.space_id)
        runtime.ensure(
            CellSpec(
                kind="org",
                binding=owned.binding,
                scope=owned.scope,
                name=owned.name,
                members=owned.members,
                params=owned.params,
            )
        )
        runtime.run_once()
        registry = app._cell_actor_registry
        assert registry is not None
        wait_until(lambda: registry.snapshot(owned.cell_id) is not None)
        snapshot = registry.snapshot(owned.cell_id)
        assert snapshot is not None
        assert snapshot.state is CellActorState.ACTIVE
        assert snapshot.entry_member is None
        foreign_id = keyed_cell_id("org", BOB, "sp-bob")
        assert registry.snapshot(foreign_id) is None
    finally:
        close_cell_runtime(app)
        assert app._actor_hosts.shutdown()
