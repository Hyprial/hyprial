"""Batch claims must not outlive delivery ownership or connector generations."""

from dataclasses import replace
import time

import pytest

from hyprial.daemon.impl.inbox.contracts.api import InboxMessage
from hyprial.daemon.impl.operations.top import build_top_snapshot
from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_ports import (
    CloseTurnPumpCommand,
    EnqueueTurnCommand,
    SteerTurnCommand,
    TurnDeliveryProjection,
    TurnResultProjection,
    TurnSteerIoCompleted,
)
from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_runtime import TurnRuntime
from tests.identity.providers.lifecycle.availability.support import WORKER, _request
from .test_batch_failure_fanout import _arrange


BATCH_CLAIM_STALE_MS = 600_000


def _wait(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


def _big_request(message_id: str, *, created_at_ms: int = 1_000) -> InboxMessage:
    return replace(
        _request(message_id, created_at_ms=created_at_ms),
        payload=b'{"message":"' + b"x" * 9_000 + b'"}',
    )


def test_oversized_backlog_spills_and_is_offered(tmp_path):
    inbox, harnesses, clock, bridge = _arrange(tmp_path)
    home = tmp_path / "agent-home"
    (home / "state").mkdir(parents=True)
    bridge._batch_actor_home = lambda actor: home
    bridge.start()
    try:
        for i in range(2):
            assert inbox.receive(_big_request(f"big-{i}")).accepted
        bridge.on_timer()
        assert len(harnesses.batch_deliveries) == 1
        delivery = harnesses.batch_deliveries[0]
        assert delivery.message_ids == ("big-0", "big-1")
        assert "inbox-batches" in delivery.message
        assert inbox.has_fetched("big-0") and inbox.has_fetched("big-1")
    finally:
        bridge.stop()
        inbox.close()


def test_oversized_backlog_without_home_falls_back_to_scalar(tmp_path):
    inbox, harnesses, clock, bridge = _arrange(tmp_path)
    logs = []
    bridge._logger = lambda level, component, event, **fields: logs.append((event, fields))
    bridge._batch_actor_home = lambda actor: None
    bridge.start()
    try:
        for i in range(3):
            assert inbox.receive(_big_request(f"big-{i}")).accepted
        bridge.on_timer()
        deliveries = harnesses.batch_deliveries
        assert deliveries, "oversized backlog must still reach the worker"
        assert all(not d.message_ids and d.batch_cursor is None for d in deliveries)
        fallback = [fields for event, fields in logs if event == "harness.batch.spill_fallback"]
        assert fallback
        assert fallback[0]["agent"] == WORKER
        assert fallback[0]["errorType"] == "SpillUnavailable"
    finally:
        bridge.stop()
        inbox.close()


@pytest.mark.parametrize("accepted, consumed", [(False, False), (True, True), (True, False)])
def test_running_turn_queued_batch_steer_then_successor(tmp_path, accepted, consumed):
    inbox, harnesses, clock, bridge = _arrange(tmp_path)
    started, events = [], []
    def io_steer(generation, version, correlation, delivery_id, message):
        turn.complete_steer(
            generation=generation, version=version, correlation_id=correlation,
            delivery_id=delivery_id, accepted=accepted, consumed=consumed,
        )

    turn = TurnRuntime(
        name="batch-claim-steer",
        capacity=8,
        io_submit=lambda generation, version, delivery: started.append(delivery.delivery_id),
        io_interrupt=lambda *args: None,
        io_steer=io_steer,
        io_close=lambda *args: None,
        io_abort=lambda generation, ready: ready(),
        io_failure_decision=lambda *args: None,
        event_sink=events.append,
    )
    original_dispatch = harnesses.dispatch

    def dispatch(name, delivery):
        result = original_dispatch(name, delivery)
        if result:
            turn.submit(EnqueueTurnCommand(delivery.delivery_id, TurnDeliveryProjection(
                delivery_id=delivery.delivery_id,
                conversation_id=delivery.conversation_id,
                sender=delivery.sender,
                recipient=delivery.recipient,
                message=delivery.message,
                message_ids=delivery.message_ids,
            )))
        return result

    def steer(name, delivery_id, timeout=5):
        turn.submit(SteerTurnCommand("steer", delivery_id))
        _wait(lambda: any(isinstance(e, TurnSteerIoCompleted) for e in events))
        return accepted

    harnesses.dispatch = dispatch
    harnesses.steer = steer
    bridge.start()
    try:
        turn.submit(EnqueueTurnCommand("running", TurnDeliveryProjection(
            "running", "conversation", "sender", WORKER, "running prompt"
        )))
        _wait(lambda: started == ["running"])
        for mid in ("queued-1", "queued-2"):
            assert inbox.receive(_request(mid)).accepted
        assert bridge.on_timer().harness_deliveries == 1
        attempt = bridge._inflight["queued-1"]
        assert inbox.ack(WORKER, "queued-2").acknowledged
        assert bridge._steer_stalled_delivery(attempt) is accepted
        if accepted:
            assert not bridge._batch_claims
        else:
            assert bridge._batch_claims["squire"] == ("queued-1", "queued-2")
        turn.complete_io(
            generation=next(e.generation for e in events if isinstance(e, TurnSteerIoCompleted)), version=0,
            result=TurnResultProjection("running", WORKER, "completed"),
        )
        _wait(lambda: bool(turn.read_results()))
        assert inbox.receive(_request("successor")).accepted
        bridge.on_timer()
        if accepted:
            _wait(lambda: started == ["running", "successor"] if consumed else started == ["running", "queued-1"])
            assert [d.message_ids for d in harnesses.batch_deliveries] == [
                ("queued-1", "queued-2"), ("successor",)
            ]
        else:
            _wait(lambda: started == ["running", "queued-1"])
            assert len(harnesses.batch_deliveries) == 1
    finally:
        turn.submit(CloseTurnPumpCommand("close", deadline_ms=100))
        assert turn.drain(1)
        bridge.stop()
        inbox.close()


@pytest.mark.parametrize("mode", ["stop", "generation", "consumed", "stale"])
def test_claim_self_heals(tmp_path, mode):
    inbox, harnesses, clock, bridge = _arrange(tmp_path)
    generation = [1]
    harnesses.streaming_generations = lambda: {"squire": generation[0]}
    bridge.start()
    try:
        assert inbox.receive(_request("old")).accepted
        bridge.on_timer()
        assert bridge._batch_claims["squire"] == ("old",)
        if mode == "stop":
            harnesses.actors = ()
        elif mode == "generation":
            generation[0] = 2
        elif mode == "consumed":
            assert inbox.ack(WORKER, "old").acknowledged
        else:
            clock["now"] += 3_600_000
        bridge.on_timer()
        assert not bridge._batch_claims
        harnesses.actors = ("squire",)
        assert inbox.receive(_request("new")).accepted
        bridge.on_timer()
        assert harnesses.batch_deliveries[-1].message_ids == ("new",)
    finally:
        bridge.stop()
        inbox.close()


def test_claim_diagnostics_and_progress_lease(tmp_path):
    inbox, harnesses, clock, bridge = _arrange(tmp_path)
    logs = []
    bridge._logger = lambda level, component, event, **fields: logs.append((event, fields))
    bridge.start()
    try:
        assert inbox.receive(_request("first")).accepted
        assert inbox.receive(_request("second")).accepted
        bridge.on_timer()
        clock["now"] += BATCH_CLAIM_STALE_MS - 1
        bridge._inflight["second"] = replace(
            bridge._inflight["second"], last_progress_ms=clock["now"]
        )
        clock["now"] += 2
        bridge.on_timer()
        assert bridge._batch_claims["squire"] == ("first", "second")
        snapshot = build_top_snapshot(
            state_dir=tmp_path, owner="op", node_id="node-reg", epoch="test",
            daemon_pid=1, connectors=[{"actor": WORKER, "name": "squire", "running": True}],
            pending={WORKER: (2, 1_000)}, stranded=[], now_ms=clock["now"],
            batch_claims=bridge.batch_claim_details(clock["now"]),
        )
        detail = snapshot["actors"][0]["stateDetail"]
        assert detail["claimedBy"] == ["first", "second"]
        assert detail["claimAgeMs"] == BATCH_CLAIM_STALE_MS + 1
        assert inbox.ack(WORKER, "first").acknowledged
        bridge.on_timer()
        assert bridge._batch_claims
        assert inbox.ack(WORKER, "second").acknowledged
        bridge.on_timer()
        assert bridge.batch_claim_details(clock["now"]) == {}
        claim_logs = [(event, fields) for event, fields in logs if event.startswith("dispatch.batch_claim.")]
        assert [event for event, _ in claim_logs] == ["dispatch.batch_claim.create", "dispatch.batch_claim.drop"]
        assert claim_logs[-1][1] == {
            "agent": WORKER, "members": ["first", "second"], "reason": "members-settled"
        }
    finally:
        bridge.stop()
        inbox.close()


def test_steer_release_does_not_repeat_deferred_fetch_members(tmp_path, monkeypatch):
    inbox, harnesses, clock, bridge = _arrange(tmp_path)
    bridge.start()
    try:
        assert inbox.receive(_request("steered")).accepted
        original_fetch = inbox.fetch_pending

        def unavailable(*args, **kwargs):
            raise OSError("injected fetch failure")

        monkeypatch.setattr(inbox, "fetch_pending", unavailable)
        bridge.on_timer()
        harnesses.steer_returns = True
        assert bridge._steer_stalled_delivery(bridge._inflight["steered"])
        harnesses.enqueued.clear()
        assert inbox.receive(_request("next")).accepted
        bridge.on_timer()
        assert [d.message_ids for d in harnesses.batch_deliveries] == [("steered",), ("next",)]
        assert bridge._steer_stalled_delivery(bridge._inflight["next"])
        harnesses.enqueued.clear()
        bridge.on_timer()
        assert len(harnesses.batch_deliveries) == 2
        monkeypatch.setattr(inbox, "fetch_pending", original_fetch)
        bridge.on_timer()
        assert inbox.has_fetched("steered") and inbox.has_fetched("next")
        assert not bridge._pending_batch_fetches
        assert len(harnesses.batch_deliveries) == 2
    finally:
        bridge.stop()
        inbox.close()


def test_refused_batch_stays_dispatchable(tmp_path):
    inbox, harnesses, clock, bridge = _arrange(tmp_path)
    real_dispatch = harnesses.dispatch
    refusal = {"active": True}

    def dispatch(name, delivery):
        if refusal["active"]:
            return False
        return real_dispatch(name, delivery)

    harnesses.dispatch = dispatch
    bridge.start()
    try:
        assert inbox.receive(_request("retry")).accepted
        bridge.on_timer()
        assert not bridge._batch_claims
        assert not bridge._pending_batch_claims
        assert not bridge._batch_claim_metadata
        assert not inbox.has_fetched("retry")
        refusal["active"] = False
        bridge.on_timer()
        assert harnesses.batch_deliveries[-1].message_ids == ("retry",)
    finally:
        bridge.stop()
        inbox.close()


@pytest.mark.parametrize("mode", ["generation", "stale"])
def test_pending_offer_claim_recovers(tmp_path, mode):
    inbox, harnesses, clock, bridge = _arrange(tmp_path)
    generation = [1]
    harnesses.streaming_generations = lambda: {"squire": generation[0]}
    bridge.start()
    try:
        message = _request("pending")
        assert inbox.receive(message).accepted
        bridge._pending_batch_claims["squire"] = (("pending",), (message,))
        bridge._begin_batch_claim("squire", ("pending",))
        if mode == "generation":
            generation[0] = 2
        else:
            clock["now"] += BATCH_CLAIM_STALE_MS
        bridge._reconcile_harness_actors()
        assert not bridge._pending_batch_claims
        assert not bridge._batch_claim_metadata
        bridge.on_timer()
        assert harnesses.batch_deliveries[-1].message_ids == ("pending",)
    finally:
        bridge.stop()
        inbox.close()
