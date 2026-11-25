"""Cell terminal routing decisions, snapshot projection and next-hop envelopes (I5-4)."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.cells.actor.actor import CellActorCore, mint_epoch
from hyprial.daemon.impl.cells.actor.contracts import CellActorState
from hyprial.daemon.impl.cells.actor.routing import (
    CELL_RETIRED,
    CELL_WORK_REFUSED,
    CellTerminalAction,
    resolve_terminal_routing,
)
from hyprial.daemon.impl.cells.actor.snapshot import CellActorSnapshot
from hyprial.daemon.impl.cells.structure import StructureConnection
from hyprial.daemon.impl.composition.cell_delivery.headless import HeadlessCellDelivery
from tests.daemon.cells.actor.actor_support import Clock, agent_cell, agent_id


def _snapshot(
    *,
    state: CellActorState = CellActorState.ACTIVE,
    entry: str | None = "router",
    connections: tuple[StructureConnection, ...] = (
        StructureConnection("router", frozenset({"code"}), "coder"),
        StructureConnection("coder", frozenset({"review"}), "reviewer"),
    ),
    fail_open: str | None = "fallback",
    effective_revision: int = 1,
) -> CellActorSnapshot:
    return CellActorSnapshot(
        cell_id="c.route",
        epoch="ce-1",
        revision=1,
        effective_revision=effective_revision,
        entry_member=entry,
        state=state,
        connections=connections,
        fail_open=fail_open,
    )


def test_actor_core_snapshot_projects_connections_and_fail_open() -> None:
    actor = CellActorCore(cell_id="c.core", epoch=mint_epoch(), clock_ms=Clock())
    cell = agent_cell(
        agent_id(),
        harness=("router", "coder", "fallback"),
        params={
            "cell.entry": "router",
            "cell.connections": [{"from": "router", "on": "code", "to": "coder"}],
            "cell.failOpen": "fallback",
        },
    )
    assert actor.apply_document(cell)
    snapshot = actor.snapshot()
    assert snapshot.state is CellActorState.ACTIVE
    assert snapshot.connections == (
        StructureConnection("router", frozenset({"code"}), "coder"),
    )
    assert snapshot.fail_open == "fallback"


def test_resolve_next_follows_matching_connection_and_unknown_label_uses_fail_open() -> None:
    snapshot = _snapshot()

    matched = resolve_terminal_routing(
        snapshot, from_member="router", outcome="next", label="code", hop_index=0
    )
    assert matched.action is CellTerminalAction.HOP
    assert matched.member == "coder"
    assert matched.hop_index == 1
    assert matched.used_fail_open is False

    unknown = resolve_terminal_routing(
        snapshot,
        from_member="router",
        outcome="next",
        label="unrecognized-label",
        hop_index=1,
    )
    assert unknown.action is CellTerminalAction.HOP
    assert unknown.member == "fallback"
    assert unknown.hop_index == 2
    assert unknown.used_fail_open is True


def test_resolve_next_without_match_or_fail_open_becomes_done() -> None:
    snapshot = _snapshot(fail_open=None)
    decision = resolve_terminal_routing(
        snapshot, from_member="router", outcome="next", label="unmatched", hop_index=0
    )
    assert decision.action is CellTerminalAction.DONE
    assert decision.member is None


def test_resolve_refused_never_routes_even_with_connections_and_fail_open() -> None:
    snapshot = _snapshot()
    decision = resolve_terminal_routing(
        snapshot, from_member="router", outcome="refused", label="code", hop_index=0
    )
    assert decision.action is CellTerminalAction.REFUSED
    assert decision.reason == CELL_WORK_REFUSED
    assert decision.member is None


def test_resolve_empty_stays_pending_and_retired_uses_internal_cell_retired() -> None:
    empty_snapshot = _snapshot(state=CellActorState.EMPTY, entry=None, connections=(), fail_open=None)
    pending = resolve_terminal_routing(
        empty_snapshot, from_member="router", outcome="next", label="code", hop_index=0
    )
    assert pending.action is CellTerminalAction.PENDING

    missing = resolve_terminal_routing(
        None, from_member="router", outcome="next", label="code", hop_index=0
    )
    assert missing.action is CellTerminalAction.PENDING

    retired_snapshot = _snapshot(state=CellActorState.RETIRED)
    retired = resolve_terminal_routing(
        retired_snapshot, from_member="router", outcome="next", label="code", hop_index=0
    )
    assert retired.action is CellTerminalAction.RETIRED
    assert retired.reason == CELL_RETIRED


def test_resolve_done_completes_without_hop() -> None:
    done = resolve_terminal_routing(
        _snapshot(), from_member="router", outcome="done", hop_index=0
    )
    assert done.action is CellTerminalAction.DONE


def test_headless_port_builds_next_hop_envelope_with_incremented_hop_index() -> None:
    snapshot = _snapshot()
    port = HeadlessCellDelivery(
        lambda worker: "c.route" if worker in {"router", "coder"} else None,
        lambda cell_id: snapshot if cell_id == "c.route" else None,
    )
    first_delivery = HarnessDelivery(
        "m-1", "conv-1", "user:alice", "router", "payload"
    )
    first_envelope = port.headless_envelope("router", first_delivery)
    assert first_envelope is not None and first_envelope.hop_index == 0

    next_delivery = HarnessDelivery(
        "m-1::hop::1", "conv-1", "user:alice", "coder", "step output"
    )
    route = port.next_hop_route(
        "router",
        delivery=next_delivery,
        outcome="next",
        label="code",
        stamp=first_envelope.stamp,
        hop_index=first_envelope.hop_index,
    )
    assert route.action == "hop"
    assert route.envelope is not None
    assert route.envelope.hop_index == 1
    assert route.envelope.stamp.member == "coder"
    assert route.envelope.stamp.cell_id == "c.route"
    assert route.envelope.recipient == "coder"

    unknown_route = port.next_hop_route(
        "router",
        delivery=HarnessDelivery(
            "m-1::hop::2", "conv-1", "user:alice", "router", "step output"
        ),
        outcome="next",
        label="unknown-route-label",
        stamp=route.envelope.stamp,
        hop_index=route.envelope.hop_index,
    )
    assert unknown_route.action == "hop"
    assert unknown_route.used_fail_open is True
    assert unknown_route.envelope is not None
    assert unknown_route.envelope.hop_index == 2
    assert unknown_route.envelope.stamp.member == "fallback"

    with pytest.raises(TypeError, match="outcome"):
        resolve_terminal_routing(
            snapshot, from_member="router", outcome="bogus", label="code"
        )


def test_runtime_bridge_routes_hops_handles_empty_retired_and_refused(tmp_path) -> None:
    import time

    from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
    from hyprial.daemon.impl.desired_state import DesiredStateStore
    from hyprial.daemon.impl.inbox import (
        DeliveryLifecycle,
        InboxMessage,
        InboxService,
        MemoryDeliveryTransport,
    )
    from hyprial.daemon.impl.runtime import DaemonEventBridge

    class _Harnesses:
        def __init__(self) -> None:
            self.results: list[HarnessResult] = []
            self.dispatched: list[object] = []

        def drain_results(self) -> tuple[HarnessResult, ...]:
            drained, self.results = tuple(self.results), []
            return drained

        def dispatch(self, envelope: object) -> bool:
            self.dispatched.append(envelope)
            return True

        def streaming_actors(self) -> tuple[str, ...]:
            return ("router", "coder", "fallback")

    state_by_cell: dict[str, CellActorSnapshot] = {"c.route": _snapshot()}
    port = HeadlessCellDelivery(
        lambda worker: "c.route" if "router" in worker or worker in {"router", "coder", "fallback"} else None,
        lambda cid: state_by_cell.get(cid),
    )
    inbox = InboxService(
        tmp_path / "inbox.sqlite3", MemoryDeliveryTransport(), node_id="node-route"
    )
    try:
        harnesses = _Harnesses()
        bridge = DaemonEventBridge(
            state_dir=tmp_path,
            node_id="node-route",
            desired_state=DesiredStateStore(tmp_path),
            transport=object(),  # type: ignore[arg-type]
            inbox=inbox,
            harnesses=harnesses,  # type: ignore[arg-type]
            presence=None,  # type: ignore[arg-type]
            delivery=None,  # type: ignore[arg-type]
            cell_delivery=port,
        )
        router_uri = "agent:alice:node-route:router"
        sender_uri = "agent:alice:node-route:asker"
        now_ms = int(time.time() * 1000)

        def seed(msg_id: str, *, intent: str = "event") -> None:
            assert inbox.receive(
                InboxMessage(
                    message_id=msg_id,
                    conversation_id="conv-1",
                    sender=sender_uri,
                    recipient=router_uri,
                    payload=b'{"message":"work"}',
                    intent=intent,
                    lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                    created_at_ms=now_ms,
                ),
                now_ms=now_ms,
            ).accepted

        seed("m-hop")
        harnesses.results.append(
            HarnessResult(
                "m-hop",
                router_uri,
                HarnessResultStatus.COMPLETED,
                output="pass to coder",
                route_outcome="next",
                route_label="code",
            )
        )
        bridge._complete_harness_results()
        assert len(harnesses.dispatched) == 1
        hop_env = harnesses.dispatched[-1]
        assert hop_env.hop_index == 1
        assert hop_env.stamp.member == "coder"
        assert [m.message_id for m in inbox.pending_messages(router_uri)] == ["m-hop"]

        harnesses.results.append(
            HarnessResult(
                hop_env.delivery_id,
                "coder",
                HarnessResultStatus.COMPLETED,
                output="all good",
                route_outcome="done",
            )
        )
        assert bridge._complete_harness_results() >= 1
        assert [m.message_id for m in inbox.pending_messages(router_uri)] == []

        seed("m-refused")
        harnesses.results.append(
            HarnessResult(
                "m-refused",
                router_uri,
                HarnessResultStatus.COMPLETED,
                route_outcome="refused",
            )
        )
        bridge._complete_harness_results()
        settlement = inbox.harness_failure_settlement("m-refused")
        assert settlement is not None
        assert settlement.terminal is True
        assert settlement.terminal_reason == CELL_WORK_REFUSED
        assert len(harnesses.dispatched) == 1

        seed("m-empty")
        state_by_cell["c.route"] = _snapshot(
            state=CellActorState.EMPTY, entry=None, connections=(), fail_open=None
        )
        harnesses.results.append(
            HarnessResult(
                "m-empty",
                router_uri,
                HarnessResultStatus.COMPLETED,
                route_outcome="next",
                route_label="code",
            )
        )
        bridge._complete_harness_results()
        assert [m.message_id for m in inbox.pending_messages(router_uri)] == ["m-empty"]
        assert inbox.harness_failure_settlement("m-empty") is None

        seed("m-retired")
        state_by_cell["c.route"] = _snapshot(state=CellActorState.RETIRED)
        harnesses.results.append(
            HarnessResult(
                "m-retired",
                router_uri,
                HarnessResultStatus.COMPLETED,
                route_outcome="next",
                route_label="code",
            )
        )
        bridge._complete_harness_results()
        retired_settlement = inbox.harness_failure_settlement("m-retired")
        assert retired_settlement is not None
        assert retired_settlement.terminal is True
        assert retired_settlement.terminal_reason == CELL_RETIRED

        state_by_cell["c.route"] = _snapshot()
        seed("m-claimed")
        assert bridge._settle_claimed_result_member(
            HarnessResult(
                "m-claimed",
                router_uri,
                HarnessResultStatus.COMPLETED,
                output="to fallback",
                route_outcome="next",
                route_label="unknown-via-claimed",
            )
        )
        claimed_env = harnesses.dispatched[-1]
        assert claimed_env.hop_index == 1
        assert claimed_env.stamp.member == "fallback"
        assert bridge._settle_claimed_result_member(
            HarnessResult(
                claimed_env.delivery_id,
                "fallback",
                HarnessResultStatus.COMPLETED,
                output="done",
                route_outcome="done",
            )
        )
        assert [m.message_id for m in inbox.pending_messages(router_uri)] == ["m-empty"]
    finally:
        inbox.close()

def test_runtime_bridge_next_without_cell_delivery_port_fails_closed_pending(
    tmp_path,
) -> None:
    import time

    from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
    from hyprial.daemon.impl.desired_state import DesiredStateStore
    from hyprial.daemon.impl.inbox import (
        DeliveryLifecycle,
        InboxMessage,
        InboxService,
        MemoryDeliveryTransport,
    )
    from hyprial.daemon.impl.runtime import DaemonEventBridge

    class _Harnesses:
        def __init__(self) -> None:
            self.results: list[HarnessResult] = []

        def drain_results(self) -> tuple[HarnessResult, ...]:
            drained, self.results = tuple(self.results), []
            return drained

        def streaming_actors(self) -> tuple[str, ...]:
            return ("router",)

    class _PortWithoutRoute:
        def headless_envelope(self, worker: str, delivery: object) -> None:
            return None

        def steer_envelope(self, stamp: object, delivery_id: str) -> None:
            return None

    router_uri = "agent:alice:node-fc:router"
    sender_uri = "agent:alice:node-fc:asker"
    inbox = InboxService(
        tmp_path / "inbox.sqlite3", MemoryDeliveryTransport(), node_id="node-fc"
    )
    try:
        now_ms = int(time.time() * 1000)

        def seed(msg_id: str) -> InboxMessage:
            msg = InboxMessage(
                message_id=msg_id,
                conversation_id="conv-1",
                sender=sender_uri,
                recipient=router_uri,
                payload=b'{"message":"work"}',
                intent="event",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=now_ms,
            )
            assert inbox.receive(msg, now_ms=now_ms).accepted
            return msg

        for port_label, port in (
            ("none", None),
            ("no-next-hop", _PortWithoutRoute()),
        ):
            harnesses = _Harnesses()
            bridge = DaemonEventBridge(
                state_dir=tmp_path,
                node_id="node-fc",
                desired_state=DesiredStateStore(tmp_path),
                transport=object(),  # type: ignore[arg-type]
                inbox=inbox,
                harnesses=harnesses,  # type: ignore[arg-type]
                presence=None,  # type: ignore[arg-type]
                delivery=None,  # type: ignore[arg-type]
                cell_delivery=port,  # type: ignore[arg-type]
            )
            drain_id = f"m-drain-{port_label}"
            claim_id = f"m-claim-{port_label}"
            drain_msg = seed(drain_id)
            claim_msg = seed(claim_id)
            assert bridge._claim_scalar_origin("router", drain_msg)
            assert bridge._claim_scalar_origin("router", claim_msg)

            harnesses.results.append(
                HarnessResult(
                    drain_id,
                    router_uri,
                    HarnessResultStatus.COMPLETED,
                    output="next output",
                    route_outcome="next",
                    route_label="code",
                )
            )
            assert bridge._complete_harness_results() == 0
            assert bridge._settle_claimed_result_member(
                HarnessResult(
                    claim_id,
                    router_uri,
                    HarnessResultStatus.COMPLETED,
                    output="next output",
                    route_outcome="next",
                    route_label="code",
                )
            )

            pending_ids = {m.message_id for m in inbox.pending_messages(router_uri)}
            assert drain_id in pending_ids
            assert claim_id in pending_ids
            assert inbox.harness_failure_settlement(drain_id) is None
            assert inbox.harness_failure_settlement(claim_id) is None
            assert bridge._claim_scalar_origin("router", drain_msg)
            assert bridge._claim_scalar_origin("router", claim_msg)
            bridge._release_origin_claims_if_present((drain_id, claim_id))

        with pytest.raises(ValueError, match="route_label"):
            HarnessResult(
                "m-bad-next",
                router_uri,
                HarnessResultStatus.COMPLETED,
                route_outcome="next",
                route_label=None,
            )
    finally:
        inbox.close()
