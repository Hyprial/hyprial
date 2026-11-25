"""Admission judged by one published snapshot (cell model I5-2).

This file is owned by the first I5-2 slice alone: it fixes the judgment table a
later bridge slice consumes — one pure read of ``CellActorSnapshot`` decides
whether headless work may start and names the entry member — and the registry
evidence that quarantine degrades the entry to ``None`` and that a supervisor
restart republishes an equivalent entry.  Nothing here drives delivery.
"""

from __future__ import annotations

from hyprial.daemon.impl.cells.actor import (
    CellActorRegistry,
    CellActorSnapshot,
    CellActorState,
    headless_admission,
)
from hyprial.kernel import (
    ActorEngineConfig,
    ActorState,
    STATE_AUTHORITY,
    SupervisionPolicy,
)
from hyprial.kernel.impl.actor_runtime.contracts import ActorFailedError
from tests.daemon.cells.actor.actor_support import (
    Log,
    PoisonExtension,
    agent_cell,
    agent_id,
    host as actor_host,
    wait_until,
)
from tests.daemon.cells.actor.test_registry import Runtime

CELL_GROUP = "cell"


def judged(state: CellActorState, entry_member: str | None) -> CellActorSnapshot:
    return CellActorSnapshot(
        cell_id="c.judged", epoch="ce-1", state=state, entry_member=entry_member
    )


def test_judgment_table_admits_only_active_with_entry() -> None:
    refused = (
        (None, "missing snapshot"),
        (judged(CellActorState.EMPTY, None), "empty"),
        (judged(CellActorState.DEGRADED, "agent:alice:mac:a"), "degraded"),
        (judged(CellActorState.RETIRING, "agent:alice:mac:a"), "retiring"),
        (judged(CellActorState.RETIRED, "agent:alice:mac:a"), "retired"),
        (judged(CellActorState.ACTIVE, None), "active without entry"),
    )
    for snapshot, _label in refused:
        decision = headless_admission(snapshot)
        assert decision.member is None
        assert decision.admitted is False
    admitted = headless_admission(judged(CellActorState.ACTIVE, "agent:alice:mac:a"))
    assert admitted.admitted is True
    assert admitted.member == "agent:alice:mac:a"


def test_registry_snapshot_admits_the_sole_harness_member() -> None:
    cell = agent_cell(agent_id())
    engine = actor_host()
    runtime = Runtime(cell)
    registry = CellActorRegistry(engine, runtime=runtime)
    try:
        registry.on_index({cell.cell_id})
        wait_until(lambda: registry.snapshot(cell.cell_id) is not None)
        value = registry.snapshot(cell.cell_id)
        assert value is not None
        assert value.state is CellActorState.ACTIVE
        assert value.entry_member == "agent:alice:mac:a"
        assert headless_admission(value).admitted is True
        assert headless_admission(value).member == "agent:alice:mac:a"
    finally:
        registry.detach()
        assert engine.shutdown()


def test_registry_snapshot_defers_until_an_entry_revision_exists() -> None:
    subject = agent_id()
    first = "agent:alice:mac:a"
    second = "agent:alice:mac:b"
    without_entry = agent_cell(subject, harness=(first, second))
    engine = actor_host()
    runtime = Runtime(without_entry)
    registry = CellActorRegistry(engine, runtime=runtime)
    try:
        registry.on_index({without_entry.cell_id})
        wait_until(
            lambda: (value := registry.snapshot(without_entry.cell_id)) is not None
            and value.state is CellActorState.DEGRADED
        )
        value = registry.snapshot(without_entry.cell_id)
        assert value is not None
        assert value.reason == "no-entry"
        assert value.entry_member is None
        assert headless_admission(value).admitted is False

        runtime.cell = agent_cell(
            subject,
            harness=(first, second),
            params={"cell.entry": second},
            revision=2,
        )
        registry.on_index({without_entry.cell_id})
        wait_until(
            lambda: (value := registry.snapshot(without_entry.cell_id)) is not None
            and value.effective_revision == 2
        )
        value = registry.snapshot(without_entry.cell_id)
        assert value is not None
        assert headless_admission(value).admitted is True
        assert headless_admission(value).member == second

        runtime.cell = agent_cell(
            subject,
            harness=(first, second),
            params={"cell.entry": second, "cell.connections": "not-a-list"},
            revision=3,
        )
        registry.on_index({without_entry.cell_id})
        wait_until(
            lambda: (value := registry.snapshot(without_entry.cell_id)) is not None
            and value.revision == 3
        )
        value = registry.snapshot(without_entry.cell_id)
        assert value is not None
        assert value.state is CellActorState.DEGRADED
        assert headless_admission(value).admitted is False
    finally:
        registry.detach()
        assert engine.shutdown()


def test_quarantined_actor_degrades_the_entry_to_none() -> None:
    cell = agent_cell(agent_id())
    engine = actor_host(
        ActorEngineConfig(
            policies={
                STATE_AUTHORITY: SupervisionPolicy(0, 60.0, 0.0, 0.0, 0.0)
            }
        )
    )
    runtime = Runtime(cell)
    log = Log()
    registry = CellActorRegistry(
        engine, runtime=runtime, extensions=(PoisonExtension(),), logger=log
    )
    try:
        registry.on_index({cell.cell_id})
        wait_until(lambda: registry.snapshot(cell.cell_id) is not None)
        healthy = registry.snapshot(cell.cell_id)
        assert healthy is not None
        assert healthy.state is CellActorState.ACTIVE
        assert healthy.entry_member == "agent:alice:mac:a"
        assert headless_admission(healthy).admitted is True

        actor = engine.find(CELL_GROUP, cell.cell_id)
        assert actor is not None
        try:
            actor.ask("poison", timeout=2.0)
        except ActorFailedError:
            pass
        wait_until(lambda: actor.snapshot().state is ActorState.QUARANTINED)
        degraded = registry.snapshot(cell.cell_id)
        assert degraded is not None
        assert degraded.state is CellActorState.DEGRADED
        assert degraded.reason == "actor-quarantined"
        assert degraded.entry_member is None
        assert headless_admission(degraded).admitted is False
    finally:
        registry.detach()
        assert engine.shutdown()


def test_restart_rebuild_publishes_an_equivalent_entry_member() -> None:
    subject = agent_id()
    first = "agent:alice:mac:a"
    second = "agent:alice:mac:b"
    cell = agent_cell(
        subject, harness=(first, second), params={"cell.entry": second}
    )
    engine = actor_host()
    runtime = Runtime(cell)
    registry = CellActorRegistry(
        engine, runtime=runtime, extensions=(PoisonExtension(),)
    )
    try:
        registry.on_index({cell.cell_id})
        wait_until(lambda: registry.snapshot(cell.cell_id) is not None)
        before = registry.snapshot(cell.cell_id)
        assert before is not None
        assert before.entry_member == second
        assert headless_admission(before).admitted is True

        actor = engine.find(CELL_GROUP, cell.cell_id)
        assert actor is not None
        try:
            actor.ask("poison", timeout=2.0)
        except ActorFailedError:
            pass
        wait_until(
            lambda: (
                (after := registry.snapshot(cell.cell_id)) is not None
                and after.epoch != before.epoch
            )
        )
        after = registry.snapshot(cell.cell_id)
        assert after is not None
        assert after.epoch != before.epoch
        assert after.state is before.state
        assert after.effective_revision == before.effective_revision
        assert after.entry_member == before.entry_member
        assert headless_admission(after).admitted is True
        assert headless_admission(after).member == second
    finally:
        registry.detach()
        assert engine.shutdown()
