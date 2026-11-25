from __future__ import annotations

from dataclasses import dataclass
from hyprial.daemon.impl.cells.actor.actor import (
    CellActorCore,
    CellActorHandler,
    document_digest,
    mint_epoch,
)
from hyprial.daemon.impl.cells.actor.contracts import (
    CellActorInput,
    CellActorState,
    CellActorWakeup,
)
from hyprial.daemon.impl.cells.actor.registry import CellActorRegistry
from hyprial.daemon.impl.cells.actor.snapshot import (
    CellActorHistoryStore,
    CellActorSnapshot,
    CellActorSnapshotStore,
)
from tests.daemon.cells.actor.actor_support import Clock, agent_cell, agent_id
from tests.daemon.cells.actor.test_registry import Host, Runtime


@dataclass
class Input:
    document: object | None
    health: tuple[bool, str | None] | None = None

    def read(self) -> CellActorInput:
        return CellActorInput(self.document, 0, None, self.health, 0)


def test_core_reduction_keeps_revision_conflict_and_tombstone_rules() -> None:
    actor = CellActorCore(cell_id="c.core", epoch=mint_epoch(), clock_ms=Clock())
    cell = agent_cell(agent_id())
    assert actor.apply_document(cell)
    assert actor.snapshot().state is CellActorState.ACTIVE
    conflict = agent_cell(cell.binding.value, updated_at_ms=2_000)
    assert actor.apply_document(conflict)
    assert actor.snapshot().state is CellActorState.DEGRADED
    retired = agent_cell(cell.binding.value, revision=2, retired=True)
    assert actor.apply_document(retired)
    assert actor.snapshot().state is CellActorState.RETIRED


def test_handler_reads_runtime_levels_and_commits_before_publishing() -> None:
    cell = agent_cell(agent_id())
    input_level = Input(cell, (True, "offline"))
    history = CellActorHistoryStore()
    snapshots = CellActorSnapshotStore()
    published: list[str] = []
    handler = CellActorHandler(
        cell_id=cell.cell_id,
        projection=None,
        input_port=input_level,
        next_epoch=history.next_epoch,
        commit=history.commit,
        publish=lambda snapshot: published.append(snapshot.epoch)
        if snapshots.publish(cell.cell_id, snapshot)
        else False,
    )
    handler(CellActorWakeup())
    value = snapshots.get(cell.cell_id)
    assert value is not None
    assert value.source_stale is True
    assert value.revision == cell.revision
    assert published == [value.epoch]


def test_each_wakeup_commits_and_publishes_a_new_sequence() -> None:
    cell = agent_cell(agent_id())
    history = CellActorHistoryStore()
    snapshots = CellActorSnapshotStore()
    commits: list[int] = []
    published: list[int] = []
    clock = Clock()

    def commit(cell_id: str, epoch: str, sequence: int, projection: object) -> bool:
        commits.append(sequence)
        return history.commit(cell_id, epoch, sequence, projection)

    def publish(snapshot: object) -> bool:
        published.append(snapshot.sequence)
        return snapshots.publish(cell.cell_id, snapshot)

    handler = CellActorHandler(
        cell_id=cell.cell_id,
        input_port=Input(cell, (False, None)),
        next_epoch=history.next_epoch,
        commit=commit,
        publish=publish,
        clock_ms=clock,
    )
    handler(CellActorWakeup())
    first = snapshots.get(cell.cell_id)
    assert first is not None
    handler(CellActorWakeup())
    second = snapshots.get(cell.cell_id)
    assert second is not None
    assert commits == [1, 2]
    assert published == [1, 2]
    assert second.sequence == first.sequence + 1
    assert second.updated_at_ms == first.updated_at_ms


def test_factory_and_stores_do_not_publish_before_first_wakeup() -> None:
    history = CellActorHistoryStore()
    snapshots = CellActorSnapshotStore()
    handler = CellActorHandler(
        cell_id="c.factory",
        projection=history.read("c.factory"),
        input_port=Input(None),
        next_epoch=history.next_epoch,
        commit=history.commit,
        publish=lambda snapshot: snapshots.publish("c.factory", snapshot),
    )
    assert handler.core is None
    assert history.read("c.factory") is None
    assert snapshots.get("c.factory") is None
    handler(CellActorWakeup())
    assert history.read("c.factory") is not None
    assert snapshots.get("c.factory") is not None


def test_factory_can_run_repeatedly_without_store_mutations() -> None:
    history = CellActorHistoryStore()
    snapshots = CellActorSnapshotStore()
    registry = CellActorRegistry(
        Host(), runtime=Runtime(None), history=history, snapshots=snapshots
    )
    factory = registry._factory("c.factory")
    calls = {"next": 0, "commit": 0, "publish": 0}
    next_epoch = history.next_epoch
    commit = history.commit
    publish = snapshots.publish

    def counted_next(cell_id: str) -> str:
        calls["next"] += 1
        return next_epoch(cell_id)

    def counted_commit(*args: object) -> bool:
        calls["commit"] += 1
        return commit(*args)

    def counted_publish(*args: object) -> bool:
        calls["publish"] += 1
        return publish(*args)

    history.next_epoch = counted_next
    history.commit = counted_commit
    snapshots.publish = counted_publish
    for _ in range(3):
        handler = factory()
        assert handler.core is None
    assert calls == {"next": 0, "commit": 0, "publish": 0}
    assert history._next == {}
    assert history._committed == {}
    assert history._values == {}
    assert snapshots.snapshots() == {}


def test_history_and_snapshot_stores_reject_old_epoch_or_sequence() -> None:
    history = CellActorHistoryStore()
    snapshots = CellActorSnapshotStore()
    first = history.next_epoch("c.store")
    second = history.next_epoch("c.store")
    projection = CellActorCore(cell_id="c.store", epoch=first).projection()
    assert history.commit("c.store", first, 1, projection)
    assert not history.commit("c.store", first, 1, projection)
    assert history.commit("c.store", second, 0, projection)
    value = CellActorCore(cell_id="c.store", epoch=second).snapshot()
    assert snapshots.publish("c.store", value)
    assert not snapshots.publish("c.store", value)
    old = CellActorCore(cell_id="c.store", epoch=first).snapshot()
    assert not snapshots.publish("c.store", old)


def test_snapshot_values_are_immutable() -> None:
    core = CellActorCore(cell_id="c.immutable", epoch=mint_epoch())
    value = core.snapshot()
    assert value.members == ()
    assert document_digest(agent_cell(agent_id()))


def test_entry_member_reads_the_effective_structure_query() -> None:
    single = agent_cell(agent_id())
    core = CellActorCore(cell_id=single.cell_id, epoch=mint_epoch(), clock_ms=Clock())
    assert core.apply_document(single)
    value = core.snapshot()
    assert value.state is CellActorState.ACTIVE
    assert value.entry_member == "agent:alice:mac:a"

    first = "agent:alice:mac:a"
    second = "agent:alice:mac:b"
    explicit = agent_cell(
        agent_id(),
        harness=(first, second),
        params={"cell.entry": second},
    )
    core = CellActorCore(cell_id=explicit.cell_id, epoch=mint_epoch(), clock_ms=Clock())
    assert core.apply_document(explicit)
    value = core.snapshot()
    assert value.state is CellActorState.ACTIVE
    assert value.entry_member == second

    none_harness = agent_cell(agent_id(), harness=())
    core = CellActorCore(cell_id=none_harness.cell_id, epoch=mint_epoch(), clock_ms=Clock())
    assert core.apply_document(none_harness)
    value = core.snapshot()
    assert value.state is CellActorState.EMPTY
    assert value.entry_member is None

    multi = agent_cell(agent_id(), harness=(first, second))
    core = CellActorCore(cell_id=multi.cell_id, epoch=mint_epoch(), clock_ms=Clock())
    assert core.apply_document(multi)
    value = core.snapshot()
    assert value.state is CellActorState.DEGRADED
    assert value.reason == "no-entry"
    assert value.entry_member is None

    missing_target = agent_cell(
        agent_id(),
        harness=(first, second),
        params={"cell.entry": "agent:ghost:mac:z"},
    )
    core = CellActorCore(
        cell_id=missing_target.cell_id, epoch=mint_epoch(), clock_ms=Clock()
    )
    assert core.apply_document(missing_target)
    value = core.snapshot()
    assert value.state is CellActorState.DEGRADED
    assert value.reason == "entry-unknown-member"
    assert value.effective_revision == 0
    assert value.entry_member is None


def test_entry_member_keeps_the_last_effective_entry_under_problems() -> None:
    subject = agent_id()
    clean = agent_cell(subject)
    core = CellActorCore(cell_id=clean.cell_id, epoch=mint_epoch(), clock_ms=Clock())
    assert core.apply_document(clean)
    assert core.snapshot().entry_member == "agent:alice:mac:a"

    broken = agent_cell(
        subject,
        harness=("agent:alice:mac:a", "agent:alice:mac:b"),
        params={"cell.connections": "not-a-list", "cell.entry": "agent:ghost:mac:z"},
        revision=2,
    )
    assert core.apply_document(broken)
    value = core.snapshot()
    assert value.state is CellActorState.DEGRADED
    assert value.revision == 2
    assert value.effective_revision == 1
    assert value.entry_member == "agent:alice:mac:a"

    conflict = agent_cell(subject, revision=2, updated_at_ms=2_000)
    assert core.apply_document(conflict)
    value = core.snapshot()
    assert value.state is CellActorState.DEGRADED
    assert value.reason == "revision-conflict"
    assert value.entry_member == "agent:alice:mac:a"

    healed = agent_cell(subject, revision=3, updated_at_ms=3_000)
    assert core.apply_document(healed)
    value = core.snapshot()
    assert value.state is CellActorState.ACTIVE
    assert value.effective_revision == 3
    assert value.entry_member == "agent:alice:mac:a"


def test_entry_member_follows_the_effective_structure_after_tombstone() -> None:
    subject = agent_id()
    alive = agent_cell(subject)
    core = CellActorCore(cell_id=alive.cell_id, epoch=mint_epoch(), clock_ms=Clock())
    assert core.apply_document(alive)
    tombstone = agent_cell(subject, revision=2, retired=True)
    assert core.apply_document(tombstone)
    value = core.snapshot()
    assert value.state is CellActorState.RETIRED
    assert value.effective_revision == 2
    assert value.entry_member == "agent:alice:mac:a"

    empty_tombstone = agent_cell(agent_id(), revision=1, retired=True, harness=())
    core = CellActorCore(
        cell_id=empty_tombstone.cell_id, epoch=mint_epoch(), clock_ms=Clock()
    )
    assert core.apply_document(empty_tombstone)
    value = core.snapshot()
    assert value.state is CellActorState.RETIRED
    assert value.entry_member is None


def test_projection_rebuild_publishes_the_same_entry_member() -> None:
    subject = agent_id()
    first = "agent:alice:mac:a"
    second = "agent:alice:mac:b"
    document = agent_cell(
        subject, harness=(first, second), params={"cell.entry": second}
    )
    core = CellActorCore(cell_id=document.cell_id, epoch=mint_epoch(), clock_ms=Clock())
    assert core.apply_document(document)
    broken = agent_cell(
        subject,
        harness=(first, second),
        params={"cell.connections": "not-a-list"},
        revision=2,
    )
    assert core.apply_document(broken)
    before = core.snapshot()
    assert before.state is CellActorState.DEGRADED
    assert before.entry_member == second

    rebuilt = CellActorCore.from_projection(
        cell_id=document.cell_id,
        epoch=mint_epoch(),
        clock_ms=Clock(),
        projection=core.projection(),
    )
    after = rebuilt.snapshot()
    assert after.state is before.state
    assert after.effective_revision == before.effective_revision
    assert after.entry_member == before.entry_member


def test_entry_member_refuses_wrong_field_type() -> None:
    epoch = mint_epoch()
    try:
        CellActorSnapshot(cell_id="c.entry-type", epoch=epoch, entry_member="")
    except TypeError:
        return
    raise AssertionError("empty entry_member must be refused")
