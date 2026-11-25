"""Deterministic tests of the M′ envelope builder and delivery port (I5-2 B/C).

Every case maps to CM-CONTRACT §9.6 and the M-F sequences it names; the
mapping is recorded per test.  The port is driven over fake snapshot sources
so each snapshot read is an explicit, observable decision point.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.cells.actor.contracts import (
    CellDispatchStamp,
    DISPATCH_STEER,
    DISPATCH_TURN,
)
from hyprial.daemon.impl.cells.actor.snapshot import CellActorSnapshot
from hyprial.daemon.impl.cells.actor.snapshot import CellActorState
from hyprial.daemon.impl.composition.cell_delivery.headless import (
    HeadlessCellDelivery,
    build_headless_envelope,
    build_steer_envelope,
)


def _snapshot(
    *,
    cell_id: str = "c.test",
    epoch: str = "ce-1",
    state: CellActorState = CellActorState.ACTIVE,
    entry: str | None = "agent:op:node:worker",
    revision: int = 7,
    effective_revision: int | None = None,
) -> CellActorSnapshot:
    return CellActorSnapshot(
        cell_id=cell_id,
        epoch=epoch,
        revision=revision,
        effective_revision=(
            effective_revision if effective_revision is not None else revision
        ),
        entry_member=entry,
        state=state,
    )


def _delivery(*message_ids: str) -> HarnessDelivery:
    ids = message_ids or ("m-1",)
    return HarnessDelivery(
        ids[0], "conversation-1", "sender", "agent:op:node:worker", "work",
        message_ids=tuple(message_ids),
    )


class _Cells:
    """A mutable snapshot source: what the registry would publish, per cell."""

    def __init__(self, snapshots: dict[str, CellActorSnapshot | None]) -> None:
        self.snapshots = snapshots
        self.reads: list[str] = []

    def cell(self, cell_id: str) -> CellActorSnapshot | None:
        self.reads.append(cell_id)
        return self.snapshots.get(cell_id)

    def port(self, _worker: str = "worker", cell_id: str = "c.test") -> HeadlessCellDelivery:
        return HeadlessCellDelivery(lambda name: cell_id if name == "worker" else None, self.cell)


def test_builder_stamps_the_snapshot_entry_and_effective_revision() -> None:
    snapshot = _snapshot(entry="agent:op:node:worker", effective_revision=5, revision=9)
    envelope = build_headless_envelope(snapshot, "agent:op:node:worker", _delivery())
    assert envelope.dispatch_kind == DISPATCH_TURN
    assert envelope.hop_index == 0
    assert envelope.stamp.cell_id == "c.test"
    assert envelope.stamp.member == "agent:op:node:worker"
    assert envelope.stamp.epoch == "ce-1"
    assert envelope.stamp.revision == 5
    assert envelope.stamp.source == "headless-claim"
    assert envelope.stamp.origin_message_ids == ("m-1",)
    assert envelope.delivery_id == "m-1"


def test_builder_takes_batch_members_as_origin_ids() -> None:
    envelope = build_headless_envelope(
        _snapshot(), "agent:op:node:worker", _delivery("m-1", "m-2", "m-3")
    )
    assert envelope.stamp.origin_message_ids == ("m-1", "m-2", "m-3")
    assert envelope.message_ids == ("m-1", "m-2", "m-3")


def test_builder_refuses_a_member_the_snapshot_did_not_publish() -> None:
    with pytest.raises(ValueError, match="snapshot's entry member"):
        build_headless_envelope(_snapshot(), "agent:op:node:other", _delivery())


def test_envelope_refuses_origin_ids_that_do_not_match_the_input() -> None:
    stamp = CellDispatchStamp(
        "c.test", "agent:op:node:worker", "ce-1", 1, ("m-2",)
    )
    with pytest.raises(TypeError, match="match the captured input"):
        from hyprial.daemon.impl.cells.actor.contracts import CellDispatchEnvelope
        CellDispatchEnvelope(stamp=stamp, input=_delivery("m-1"))


def test_envelope_refuses_a_turn_without_input() -> None:
    from hyprial.daemon.impl.cells.actor.contracts import CellDispatchEnvelope
    stamp = CellDispatchStamp("c.test", "agent:op:node:worker", "ce-1", 1, ("m-1",))
    with pytest.raises(TypeError, match="without input must be a steer"):
        CellDispatchEnvelope(stamp=stamp, input=None)


def test_port_admits_an_active_cell_and_publishes_one_read_per_call() -> None:
    cells = _Cells({"c.test": _snapshot()})
    envelope = cells.port().headless_envelope("worker", _delivery())
    assert envelope is not None
    assert envelope.stamp.member == "agent:op:node:worker"
    assert cells.reads == ["c.test"]


def test_port_defers_missing_snapshot_empty_degraded_and_retired(
) -> None:
    for value in (
        None,
        _snapshot(state=CellActorState.EMPTY, entry=None),
        _snapshot(state=CellActorState.DEGRADED),
        _snapshot(state=CellActorState.RETIRING),
        _snapshot(state=CellActorState.RETIRED),
        _snapshot(entry=None),
    ):
        cells = _Cells({"c.test": value})
        assert cells.port().headless_envelope("worker", _delivery()) is None


def test_port_defers_a_worker_without_a_cell() -> None:
    cells = _Cells({"c.test": _snapshot()})
    assert cells.port().headless_envelope("stranger", _delivery()) is None
    assert cells.reads == []


def test_port_defers_when_the_registry_read_fails() -> None:
    class _Broken:
        def cell(self, _cell_id: str) -> CellActorSnapshot | None:
            raise RuntimeError("registry unavailable")

    port = HeadlessCellDelivery(lambda name: "c.test" if name == "worker" else None, _Broken().cell)
    assert port.headless_envelope("worker", _delivery()) is None


def test_snapshot_read_is_the_linearization_point_not_the_offer_time() -> None:
    cells = _Cells({"c.test": _snapshot(entry="agent:op:node:a", revision=7)})
    port = cells.port()
    first = port.headless_envelope("worker", _delivery())
    assert first is not None and first.stamp.member == "agent:op:node:a"
    cells.snapshots["c.test"] = _snapshot(
        entry="agent:op:node:b", epoch="ce-2", revision=8
    )
    assert first.stamp.member == "agent:op:node:a"
    assert first.stamp.revision == 7
    second = port.headless_envelope("worker", _delivery("m-2"))
    assert second is not None and second.stamp.member == "agent:op:node:b"
    assert second.stamp.revision == 8


def test_empty_to_active_window_defers_then_admits() -> None:
    cells = _Cells({"c.test": _snapshot(state=CellActorState.EMPTY, entry=None)})
    port = cells.port()
    assert port.headless_envelope("worker", _delivery()) is None
    cells.snapshots["c.test"] = _snapshot()
    envelope = port.headless_envelope("worker", _delivery())
    assert envelope is not None


def test_quarantined_degraded_copy_and_stopping_registry_defer() -> None:
    quarantined = replace(
        _snapshot(), state=CellActorState.DEGRADED,
        reason="actor-quarantined", entry_member=None,
    )
    cells = _Cells({"c.test": quarantined})
    assert cells.port().headless_envelope("worker", _delivery()) is None
    stopping = _Cells({"c.test": None})
    assert stopping.port().headless_envelope("worker", _delivery()) is None


def test_steer_envelope_reuses_the_original_stamp_member() -> None:
    stamp = CellDispatchStamp(
        "c.test", "agent:op:node:a", "ce-1", 7, ("m-1", "m-2")
    )
    envelope = build_steer_envelope(stamp, "m-1")
    assert envelope.dispatch_kind == DISPATCH_STEER
    assert envelope.input is None
    assert envelope.stamp.member == "agent:op:node:a"
    assert envelope.stamp.epoch == "ce-1"
    assert envelope.stamp.revision == 7
    assert envelope.stamp.origin_message_ids == ("m-1",)


def test_steer_envelope_never_reads_a_snapshot_after_an_entry_change() -> None:
    cells = _Cells({"c.test": _snapshot(entry="agent:op:node:b", revision=9)})
    port = cells.port()
    stamp = CellDispatchStamp("c.test", "agent:op:node:a", "ce-1", 7, ("m-1",))
    envelope = port.steer_envelope(stamp, "m-1")
    assert envelope is not None
    assert envelope.stamp.member == "agent:op:node:a"
    assert envelope.stamp.revision == 7
    assert cells.reads == []


def test_steer_envelope_without_a_stamp_is_none() -> None:
    cells = _Cells({"c.test": _snapshot()})
    assert cells.port().steer_envelope(None, "m-1") is None
    assert cells.reads == []


def test_a_steer_envelope_is_exactly_no_input_and_one_origin_id() -> None:
    from hyprial.daemon.impl.cells.actor.contracts import CellDispatchEnvelope

    stamp = CellDispatchStamp("c.test", "agent:op:node:worker", "ce-1", 1, ("m-1",))
    with pytest.raises(TypeError, match="steer envelope carries no input"):
        CellDispatchEnvelope(
            stamp=stamp, input=_delivery("m-1"), dispatch_kind=DISPATCH_STEER
        )
    two = CellDispatchStamp("c.test", "agent:op:node:worker", "ce-1", 1, ("m-1", "m-2"))
    with pytest.raises(TypeError, match="exactly one delivery id"):
        CellDispatchEnvelope(
            stamp=two, input=None, dispatch_kind=DISPATCH_STEER
        )
    steered = build_steer_envelope(stamp, "m-1")
    assert steered.delivery_id == "m-1"


def test_a_stamp_rejects_every_non_headless_source() -> None:
    with pytest.raises(TypeError, match="headless work source"):
        CellDispatchStamp(
            "c.test", "agent:op:node:worker", "ce-1", 1, ("m-1",),
            source="interactive-session",
        )


def test_the_dispatch_port_never_composes_the_cell_world(monkeypatch) -> None:
    import types

    from hyprial.daemon.impl.composition import cell_delivery as package
    from hyprial.daemon.impl.composition.cells import wiring

    calls = {"count": 0}

    def counted(app: object) -> object:
        calls["count"] += 1
        return None

    monkeypatch.setattr(wiring, "cell_runtime", counted)
    package_cells = types.ModuleType("stub")
    package_cells.cell_runtime = counted
    monkeypatch.setattr(
        "hyprial.daemon.impl.composition.cells.cell_runtime", counted
    )
    app = types.SimpleNamespace(
        _cell_runtime_closed=False,
        _cell_runtime_lock=__import__("threading").Lock(),
    )
    port = package.compose_headless_cell_delivery(app)
    assert calls["count"] == 1
    delivery = HarnessDelivery("m-1", "c", "s", "w", "work")
    for _ in range(3):
        assert port.headless_envelope("worker", delivery) is None
    assert calls["count"] == 1
