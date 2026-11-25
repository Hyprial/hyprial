"""I6-2 startup adoption with the I6-5 template seed and member policy.

The registrar seeds the claude template rules into ``cell.members`` with a
daemon-authored revision; that seed declares the member and gives it a
policy.  These cases check that the seed never releases a held row, that an
adopted process which acknowledged the seeded policy is kept, and that one
which did not takes exactly one drift restart that launches the policy.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.cells.reconcile import CellReconcileState, ReconcileMemberPhase
from hyprial.daemon.impl.cells.reconcile.policy import (
    document_launch_policy,
    resolve_member_policy,
)
from hyprial.daemon.impl.cells.structure import parse_structure
from hyprial.daemon.impl.composition.cells import agents as cell_agents
from hyprial.daemon.impl.composition.cells.reconcile import (
    CellReconcileStartBarrier,
    nudge_member_reconcile,
)
from tests.daemon.cells.actor.actor_support import wait_until
from tests.daemon.cells.reconcile.startup.test_startup_adopt import _World


def _seeded(world: _World) -> Any:
    def declared() -> bool:
        world.runtime.run_once()
        cell = world.runtime.lookup(world.worker.cell_id)
        members = None if cell is None else cell.param("cell.members")
        entry = members.get(world.worker.uri) if isinstance(members, dict) else None
        return isinstance(entry, dict) and "deny" in entry

    wait_until(declared, timeout=10.0)
    cell = world.runtime.lookup(world.worker.cell_id)
    assert cell is not None
    return cell


def _ready_with(world: _World, digest: str) -> bool:
    member = world.member()
    snap = world.coordinator.snapshot(world.worker.cell_id)
    return (
        member is not None
        and member.phase is ReconcileMemberPhase.READY
        and member.policy_digest == digest
        and snap is not None
        and snap.state is CellReconcileState.ACTIVE
    )


def test_the_template_seed_never_releases_a_held_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cell_agents, "SEED_HARNESS", "seeding-disabled")
    world = _World(tmp_path, barrier=CellReconcileStartBarrier(opened=False), status="failed")
    try:
        world.barrier.open()
        wait_until(
            lambda: (snap := world.coordinator.snapshot(world.worker.cell_id)) is not None
            and snap.state is CellReconcileState.EMPTY
        )
        monkeypatch.setattr(cell_agents, "SEED_HARNESS", "claude")
        assert world.app._agent_cell_registrar.seed_for_launch("worker", "claude") != "deferred"
        _seeded(world)
        wait_until(lambda: world.member_phase(ReconcileMemberPhase.FAILED))
        assert world.member().last_error == "member-held"
        world.runtime.run_once()
        assert world.launcher.started == []
        assert world.persisted_row().status == "failed"
    finally:
        world.close()


def test_a_seed_after_the_held_member_was_planned_does_not_release_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cell_agents, "SEED_HARNESS", "seeding-disabled")
    world = _World(tmp_path, barrier=CellReconcileStartBarrier(opened=False), status="failed")
    try:
        world.declare_member()
        world.barrier.open()
        wait_until(lambda: world.member_phase(ReconcileMemberPhase.FAILED))
        assert world.member().last_error == "member-held"
        planned = world.coordinator.snapshot(world.worker.cell_id)
        assert planned is not None
        monkeypatch.setattr(cell_agents, "SEED_HARNESS", "claude")
        assert world.app._agent_cell_registrar.seed_for_launch("worker", "claude") != "deferred"
        seeded = _seeded(world)
        assert seeded.revision > planned.revision
        wait_until(
            lambda: (snap := world.coordinator.snapshot(world.worker.cell_id)) is not None
            and snap.revision == seeded.revision
        )
        world.runtime.run_once()
        assert world.member_phase(ReconcileMemberPhase.FAILED)
        assert world.member().last_error == "member-held"
        assert world.launcher.started == []
        assert world.persisted_row().status == "failed"
    finally:
        world.close()


def test_a_restored_harness_that_acknowledged_the_seeded_policy_is_adopted(
    tmp_path: Path,
) -> None:
    world = _World(tmp_path, barrier=CellReconcileStartBarrier(opened=False))
    try:
        cell = _seeded(world)
        launch, reason = document_launch_policy(cell, world.worker.uri, "claude")
        assert reason is None and launch is not None
        declared = next(
            item for item in parse_structure(cell).members if item.name == world.worker.uri
        )
        policy = resolve_member_policy("claude", declared.allow, declared.deny)
        world.policies.publish(("claude", "worker", "worker"), policy, version=cell.revision)
        world.harnesses.start(world.spec)
        assert world.harnesses.wait_ready("claude", "worker", 5.0)
        world.barrier.open()

        wait_until(lambda: _ready_with(world, launch.digest), timeout=10.0)
        assert len(world.launcher.started) == 1
        assert world.launcher.started[0][1].stops == 0
        assert world.member().policy_digest == launch.digest
    finally:
        world.close()


def test_a_restored_harness_without_the_policy_is_restarted_once_with_it(
    tmp_path: Path,
) -> None:
    world = _World(tmp_path, barrier=CellReconcileStartBarrier(opened=False))
    try:
        world.harnesses.start(world.spec)
        assert world.harnesses.wait_ready("claude", "worker", 5.0)
        cell = _seeded(world)
        launch, _reason = document_launch_policy(cell, world.worker.uri, "claude")
        assert launch is not None
        world.barrier.open()

        wait_until(lambda: _ready_with(world, launch.digest), timeout=10.0)
        assert len(world.launcher.started) == 2
        assert world.launcher.started[0][1].stops == 1
        assert world.launcher.started[1][1].running is True
        assert world.member().policy_digest == launch.digest
        world.runtime.run_once()
        assert len(world.launcher.started) == 2
    finally:
        world.close()


def test_a_launch_nudge_waits_for_the_startup_barrier() -> None:
    told: list[tuple[object, ...]] = []
    agent = SimpleNamespace(actor="worker", uri="agent:alice:mac:worker", cell_id="c.worker")
    barrier = CellReconcileStartBarrier(opened=False)
    app = SimpleNamespace(
        agents=SimpleNamespace(get=lambda name: agent if name == "worker" else None),
        _cell_runtime=SimpleNamespace(lookup=lambda cell_id: f"cell:{cell_id}"),
        _cell_reconcile_coordinator=SimpleNamespace(
            on_index=lambda cells, recheck=(): told.append((cells, recheck))
        ),
        _cell_reconcile_barrier=barrier,
    )

    nudge_member_reconcile(app, "worker")
    assert told == []

    barrier.open()
    nudge_member_reconcile(app, "worker")
    assert told == [(("cell:c.worker",), ("agent:alice:mac:worker",))]
