"""Regression vectors for settled batch replay and abandoned turn custody."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.harnesses.carrier.codex_carrier_store import CodexCarrierStore
from hyprial.daemon.impl.harnesses.carrier.interactive_carrier_runtime import (
    CarrierFetched,
)
from hyprial.daemon.impl.harnesses.codex.carrier import CodexInteractiveCarrier

ACTOR = "agent:probe:isolated:replay"
SESSION = "replay-session"


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def delivery(key="a", members=("a", "b")):
    return HarnessDelivery(
        delivery_id=key,
        conversation_id="conversation",
        sender="sender",
        recipient=ACTOR,
        message="immutable daemon batch",
        batch_cursor="cursor",
        message_ids=members,
    )


class Pump:
    def __init__(self, **kwargs):
        self.enqueued = []
        self.idle = True

    def enqueue(self, item):
        self.enqueued.append(item)
        return True

    def drain_results(self):
        return []


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home" / ".hyprial"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(tmp_path / "unused.sock"))
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")
    from hyprial.daemon.impl.harnesses.streaming.process import StreamingTurnProcess

    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.streaming.process.StreamingTurnProcess", Pump
    )
    context = SimpleNamespace(
        now=1000.0,
        pending=set(),
        calls=[],
        logs=[],
        carriers=[],
        hint_error=None,
        turn={"status": "inProgress", "items": []},
        real_pump=StreamingTurnProcess,
        listed={"ok": True, "messages": []},
        replay={"ok": True},
    )

    def request(method, params):
        context.calls.append((method, params))
        if method == "message.settle_hint":
            if context.hint_error:
                raise context.hint_error
            return {
                "ok": True,
                "pending": params["messageId"] in context.pending,
                "hint": "keep",
            }
        if method == "message.pending.list":
            return context.listed
        if method == "message.pending.batch":
            return context.replay
        return {"ok": True}

    def log(event, **fields):
        context.logs.append((event, fields))

    def read_turn(turn_id):
        if isinstance(context.turn, Exception):
            raise context.turn
        return context.turn

    def create(seed=None, started=False, logger=None):
        path = tmp_path / f"carrier-{len(context.carriers)}.sqlite3"
        store = CodexCarrierStore(path)
        if seed:
            store.record_fetched(ACTOR, SESSION, seed, "request")
            if started:
                store.record_turn_started(ACTOR, seed.delivery_id, "old-turn")
        store.close()
        carrier = CodexInteractiveCarrier(
            SimpleNamespace(pid=123, read_turn=read_turn),
            actor=ACTOR,
            session_ref=SESSION,
            cwd=tmp_path,
            command=["codex"],
            daemon_request=request,
            state_path=path,
            clock=lambda: context.now,
            logger=logger or SimpleNamespace(info=log, warn=log, error=log),
        )
        context.carriers.append(carrier)
        return carrier

    context.create = create
    yield context
    for carrier in context.carriers:
        try:
            if not isinstance(carrier._pump, Pump):
                carrier.server.read_turn = lambda turn_id: {"status": "failed"}
                wait_for(lambda: carrier._pump.idle)
                carrier._pump.stop()
            assert carrier._carrier_runtime.drain(3)
        finally:
            carrier._store.close()


@pytest.mark.parametrize("recovered", [True, False], ids=["recovery", "drain"])
def test_all_settled_fetched_is_not_reenqueued(rig, recovered):
    item = delivery()
    carrier = rig.create(item if recovered else None)
    if not recovered:
        carrier._stage_carrier_fetched(item, "request")
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    carrier._drain_carrier_effects()
    assert carrier._pump.enqueued == []
    wait_for(lambda: not carrier._store.load(ACTOR, SESSION))
    assert any(fields.get("stage") == "fetched-settlement" for _, fields in rig.logs)
    assert not any(method == "message.ack" for method, _ in rig.calls)


def test_polluted_store_self_heals_on_list_without_enqueue(rig):
    carrier = rig.create(delivery())
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    carrier._forget_gone_ids({"ok": True, "messages": []})
    wait_for(lambda: not carrier._store.load(ACTOR, SESSION))
    assert carrier._pump.enqueued == []


@pytest.mark.parametrize("unknown", [False, True], ids=["partial", "unavailable"])
def test_partial_batch_replays_only_pending_and_unknown_preserves_whole_once(
    rig, unknown
):
    rig.pending = {"b"}
    if unknown:
        rig.hint_error = ConnectionError("inbox unavailable")
    item = delivery()
    rig.replay = {
        "ok": True,
        "format": "inbox-batch-v1",
        "cursor": "cursor",
        "itemCount": 1,
        "items": [{"messageId": "b"}],
        "delivery": {"text": "only pending b"},
    }
    if unknown:
        rig.replay = {"ok": False}
    carrier = rig.create(item)
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    carrier._drain_carrier_effects()
    carrier._drain_carrier_effects()
    assert len(carrier._pump.enqueued) == 1
    injected = carrier._pump.enqueued[0]
    assert injected.message == (item.message if unknown else "only pending b")
    assert injected.model_prompt() == injected.message
    assert injected.message_ids == item.message_ids
    assert injected.delivery_id == item.delivery_id
    assert carrier._store.load(ACTOR, SESSION)[0].delivery.message_ids == ("a", "b")
    if unknown:
        assert any(
            fields.get("stage") == "fetched-settlement" for _, fields in rig.logs
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"itemCount": 1},
        {"items": "invalid"},
        {"cursor": None},
        {"delivery": {"text": "unexpected text"}},
        {"ok": False},
        {
            "itemCount": 1,
            "items": [{"messageId": "unrelated"}],
            "delivery": {"text": "unrelated batch"},
        },
    ],
)
def test_uncertain_cursor_replay_cannot_remove_or_replace_delivery(rig, changes):
    rig.pending = {"b"}
    rig.replay = {
        "ok": True,
        "format": "inbox-batch-v1",
        "cursor": "cursor",
        "itemCount": 0,
        "items": [],
        "delivery": {"text": ""},
        **changes,
    }
    item = delivery()
    carrier = rig.create(item)
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    carrier._drain_carrier_effects()
    assert carrier._pump.enqueued == [item]
    assert carrier._carrier_runtime.snapshot("a").stage == "FETCHED"
    assert any(fields.get("stage") == "fetched-replay" for _, fields in rig.logs)


def test_turn_started_deadline_releases_hold_without_ack_or_final(rig):
    rig.pending = {"a", "b"}
    carrier = rig.create(delivery(), started=True)
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    carrier._reconcile_inflight_turns()
    rig.now += 899.999
    carrier._reconcile_inflight_turns()
    assert carrier._carrier_runtime.snapshot("a").stage == "TURN_STARTED"
    rig.now += 0.001
    carrier._reconcile_inflight_turns()
    wait_for(lambda: carrier._carrier_runtime.snapshot("a").stage == "STATUS_UNKNOWN")
    assert carrier._inbox_hold_allowed()
    assert rig.pending == {"a", "b"}
    assert carrier._store.load(ACTOR, SESSION)[0].final_output is None
    assert not any(
        method in {"message.ack", "session.turn.ended"} for method, _ in rig.calls
    )
    fresh = delivery("c", ("c",))
    rig.pending.add("c")
    carrier._carrier_runtime.submit(CarrierFetched(fresh, "request"))
    wait_for(lambda: carrier._carrier_runtime.snapshot("c") is not None)
    carrier._drain_carrier_effects()
    assert carrier._pump.enqueued == [fresh]


def test_changed_turn_progress_resets_deadline_but_read_errors_do_not(rig):
    carrier = rig.create(delivery(), started=True)
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    carrier._reconcile_inflight_turns()
    rig.now += 800
    rig.turn = {"status": "inProgress", "items": [{"type": "reasoning", "text": "new"}]}
    carrier._reconcile_inflight_turns()
    rig.now += 800
    rig.turn = ConnectionError("read unavailable")
    carrier._reconcile_inflight_turns()
    assert carrier._carrier_runtime.snapshot("a").stage == "TURN_STARTED"
    rig.now += 100
    carrier._reconcile_inflight_turns()
    wait_for(lambda: carrier._carrier_runtime.snapshot("a").stage == "STATUS_UNKNOWN")


def test_real_pump_advances_after_unknown_without_native_interrupt(rig, monkeypatch):
    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.streaming.process.StreamingTurnProcess",
        rig.real_pump,
    )
    rig.pending = {"a", "b", "c"}
    carrier = rig.create(delivery())
    prompts = []
    interrupts = []

    def start_turn(prompt):
        prompts.append(prompt)
        return f"turn-{len(prompts)}"

    carrier.server.start_turn = start_turn
    carrier.server.interrupt_turn = interrupts.append
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    carrier._drain_carrier_effects()
    wait_for(lambda: carrier._carrier_runtime.snapshot("a").stage == "TURN_STARTED")
    carrier._reconcile_inflight_turns()
    rig.now += 900
    carrier._reconcile_inflight_turns()
    wait_for(lambda: carrier._carrier_runtime.snapshot("a").stage == "STATUS_UNKNOWN")
    wait_for(lambda: carrier._pump.idle)
    assert interrupts == []
    assert rig.pending == {"a", "b", "c"}
    fresh = delivery("c", ("c",))
    carrier._stage_carrier_fetched(fresh, "request")
    wait_for(lambda: carrier._carrier_runtime.snapshot("c") is not None)
    carrier._drain_carrier_effects()
    wait_for(lambda: len(prompts) == 2)
    wait_for(lambda: carrier._carrier_runtime.snapshot("c").stage == "TURN_STARTED")
    carrier._reconcile_inflight_turns()
    rig.now += 900
    carrier._reconcile_inflight_turns()
    wait_for(lambda: carrier._pump.idle)
    assert interrupts == []
    assert not any(method == "message.ack" for method, _ in rig.calls)


def test_reopened_daemon_inbox_controls_recovered_batch(rig, tmp_path):
    from hyprial.daemon.impl.inbox import (
        DeliveryLifecycle,
        InboxMessage,
        InboxService,
        MemoryDeliveryTransport,
    )

    path = tmp_path / "inbox.sqlite3"
    inbox = InboxService(path, MemoryDeliveryTransport())
    for message_id in ("a", "b", "c"):
        inbox.receive(
            InboxMessage(
                message_id=message_id,
                conversation_id="replay",
                sender="agent:sender:node:s",
                recipient=ACTOR,
                payload=b"{}",
                intent="request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                idempotency_key=None,
                created_at_ms=0,
            )
        )
    assert inbox.ack(ACTOR, "a").acknowledged
    assert inbox.ack(ACTOR, "b").acknowledged
    inbox.close()
    inbox = InboxService(path, MemoryDeliveryTransport())
    try:
        carrier = rig.create(delivery())

        def request(method, params):
            if method == "message.pending.list":
                return {
                    "ok": True,
                    "messages": [
                        {"messageId": m.message_id}
                        for m in inbox.pending_messages(ACTOR)
                    ],
                }
            assert method == "message.settle_hint"
            assert params["actor"] == ACTOR and params["sessionRef"] == SESSION
            pending = any(
                m.message_id == params["messageId"]
                for m in inbox.pending_messages(ACTOR)
            )
            return {
                "ok": True,
                "pending": pending,
                "hint": "keep" if pending else "ack",
            }

        carrier.daemon_request = request
        wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
        carrier._drain_carrier_effects()
        wait_for(lambda: not carrier._store.load(ACTOR, SESSION))
        assert carrier._pump.enqueued == []
        fresh = delivery("c", ("c",))
        carrier._stage_carrier_fetched(fresh, "request")
        wait_for(lambda: carrier._carrier_runtime.snapshot("c") is not None)
        carrier._drain_carrier_effects()
        assert carrier._pump.enqueued == [fresh]
        assert [m.message_id for m in inbox.pending_messages(ACTOR)] == ["c"]
    finally:
        inbox.close()


@pytest.mark.parametrize(
    "answer", [{}, {"ok": False, "pending": False}, {"ok": True, "pending": 0}]
)
def test_malformed_settlement_answer_cannot_delete(rig, answer):
    carrier = rig.create(delivery())
    carrier.daemon_request = lambda method, params: answer
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    carrier._drain_carrier_effects()
    assert carrier._pump.enqueued == [delivery()]
    assert carrier._store.load(ACTOR, SESSION)


@pytest.mark.parametrize(
    "listed",
    [
        {"ok": True, "messages": [{"messageId": "a", "kind": "notice"}]},
        {"ok": False, "messages": []},
        {"ok": True, "messages": [None]},
    ],
)
def test_pending_notice_or_unavailable_notice_list_retains_batch(rig, listed):
    rig.listed = listed
    carrier = rig.create(delivery())
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    carrier._drain_carrier_effects()
    assert carrier._pump.enqueued == [delivery()]
    assert carrier._store.load(ACTOR, SESSION)
    rig.listed = {"ok": True, "messages": []}
    carrier._forget_gone_ids(rig.listed)
    wait_for(lambda: not carrier._store.load(ACTOR, SESSION))


def test_started_mapping_wins_over_delayed_fetched_cleanup(rig):
    carrier = rig.create(delivery())
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    carrier._store.record_turn_started(ACTOR, "a", "turn-race")
    carrier._drain_carrier_effects()
    assert carrier._carrier_runtime.drain(3)
    assert carrier._store.load(ACTOR, SESSION)[0].turn_id == "turn-race"
    assert carrier._pump.enqueued == []


def test_long_running_turn_with_continuous_progress_is_not_abandoned(rig):
    carrier = rig.create(delivery(), started=True)
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    for step in range(8):
        rig.turn = {
            "status": "inProgress",
            "items": [{"type": "reasoning", "text": str(step)}],
        }
        carrier._reconcile_inflight_turns()
        rig.now += 800
    assert carrier._carrier_runtime.snapshot("a").stage == "TURN_STARTED"
    assert not any(
        event == "worker.carrier.turn_status_unknown" for event, _ in rig.logs
    )


def test_native_delta_activity_keeps_unchanged_thread_read_alive(rig):
    carrier = rig.create(delivery(), started=True)
    activity = [0]
    carrier.server.turn_activity_sequence = lambda turn_id: activity[0]
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    for step in range(8):
        activity[0] = step
        carrier._reconcile_inflight_turns()
        rig.now += 800
    assert carrier._carrier_runtime.snapshot("a").stage == "TURN_STARTED"
    assert not any(
        event == "worker.carrier.turn_status_unknown" for event, _ in rig.logs
    )
    rig.now += 100
    carrier._reconcile_inflight_turns()
    wait_for(lambda: carrier._carrier_runtime.snapshot("a").stage == "STATUS_UNKNOWN")


def test_socket_activity_is_turn_scoped_and_excludes_level_status():
    import threading

    from hyprial.daemon.impl.harnesses.codex.transport.connection import CodexRpcConnection

    connection = object.__new__(CodexRpcConnection)
    connection._turn_activity = {}
    connection._activity_sequence = 0
    connection._responses = {}
    connection._response_cv = threading.Condition()
    connection._listeners = ()
    connection._listener_lock = threading.Lock()
    frames = iter(
        [
            {
                "method": "item/reasoning/textDelta",
                "params": {"threadId": "t", "turnId": "a", "delta": "secret"},
            },
            {
                "method": "thread/status/changed",
                "params": {"threadId": "t", "turnId": "a", "status": "inProgress"},
            },
            {"method": "item/started", "params": {"threadId": "t", "turnId": "other"}},
            {"id": 1, "result": {}},
        ]
    )
    for frame in frames:
        connection._route(frame)
    assert connection.turn_activity_sequence("t", "a") == 1
    assert connection.turn_activity_sequence("t", "other") == 2
    assert connection.turn_activity_sequence("other-thread", "a") == 0


def test_late_final_after_unknown_is_ignored_without_reenqueue(rig):
    from hyprial.daemon.impl.harnesses.carrier.interactive_carrier_runtime import (
        CarrierFinalObserved,
    )

    rig.pending = {"a", "b"}
    carrier = rig.create(delivery(), started=True)
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    carrier._reconcile_inflight_turns()
    rig.now += 900
    carrier._reconcile_inflight_turns()
    wait_for(lambda: carrier._carrier_runtime.snapshot("a").stage == "STATUS_UNKNOWN")
    state = carrier._carrier_runtime.snapshot("a")
    carrier._carrier_runtime.submit(
        CarrierFinalObserved(state.generation, "a", "late final")
    )
    assert carrier._carrier_runtime.drain(3)
    carrier._drain_carrier_effects()
    stored = carrier._store.load(ACTOR, SESSION)[0]
    assert stored.state == "STATUS_UNKNOWN" and stored.final_output is None
    assert carrier._pump.enqueued == []
    assert rig.pending == {"a", "b"}
    assert not any(method == "message.ack" for method, _ in rig.calls)


def test_observed_final_wins_at_deadline(rig):
    carrier = rig.create(delivery(), started=True)
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    carrier._reconcile_inflight_turns()
    rig.now += 900
    rig.turn = {
        "status": "completed",
        "items": [
            {"type": "agentMessage", "phase": "final_answer", "text": "real final"}
        ],
    }
    carrier._reconcile_inflight_turns()
    wait_for(lambda: carrier._carrier_runtime.snapshot("a").stage == "FINAL_OBSERVED")
    assert carrier._store.load(ACTOR, SESSION)[0].final_output == "real final"


def test_recovery_and_cleanup_logs_reach_worker_file_without_body(rig, tmp_path):
    import json

    from hyprial.kernel import Logger

    logger = Logger.worker(tmp_path / "logs-state", runtime="codex", name=ACTOR)
    carrier = rig.create(delivery(), logger=logger)
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    carrier._drain_carrier_effects()
    wait_for(lambda: not carrier._store.load(ACTOR, SESSION))
    assert carrier._carrier_runtime.drain(3)
    carrier._drain_carrier_effects()
    text = logger.path.read_text()
    rows = [json.loads(line) for line in text.splitlines()]
    summary = next(
        row for row in rows if row["event"] == "worker.carrier.recovery_summary"
    )
    assert summary["rowCount"] == 1 and summary["stateCounts"] == {"FETCHED": 1}
    skipped = next(
        row for row in rows if row["event"] == "worker.carrier.fetched_skipped"
    )
    assert skipped["source"] == "recovery"
    assert any(row["event"] == "worker.carrier.fetched_removed" for row in rows)
    assert "immutable daemon batch" not in text


@pytest.mark.parametrize("remaining", [(), ("b",)], ids=["all-settled", "partial"])
@pytest.mark.parametrize("sweep", [False, True], ids=["queued", "swept-before-query"])
def test_batch_settled_while_waiting_in_fifo_never_reshows_settled_members(
    rig, monkeypatch, remaining, sweep
):
    from dataclasses import replace

    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.streaming.process.StreamingTurnProcess",
        rig.real_pump,
    )
    rig.pending = {"first", "a", "b"}
    first = replace(delivery("first", ("first",)), message="first prompt")
    carrier = rig.create(first)
    prompts = []

    def start_turn(prompt):
        prompts.append(prompt)
        return f"turn-{len(prompts)}"

    carrier.server.start_turn = start_turn
    carrier.server.interrupt_turn = lambda turn_id: None
    wait_for(lambda: carrier._carrier_runtime.snapshot("first") is not None)
    carrier._drain_carrier_effects()
    wait_for(lambda: carrier._carrier_runtime.snapshot("first").stage == "TURN_STARTED")
    carrier._stage_carrier_fetched(delivery(), "request")
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    carrier._drain_carrier_effects()
    assert prompts == ["first prompt"]
    rig.pending = {"first", *remaining}
    rig.replay = {
        "ok": True,
        "format": "inbox-batch-v1",
        "cursor": "cursor",
        "itemCount": len(remaining),
        "items": [{"messageId": member} for member in remaining],
        "delivery": {"text": "pending b only" if remaining else ""},
    }
    if sweep:
        carrier._forget_gone_ids({"ok": True, "messages": []})
        if not remaining:
            wait_for(lambda: carrier._carrier_runtime.snapshot("a") is None)
    rig.now += 7200
    rig.turn = {
        "status": "completed",
        "items": [{"type": "agentMessage", "phase": "final_answer", "text": "done"}],
    }
    wait_for(lambda: carrier._pump.idle)
    if remaining:
        wait_for(lambda: carrier._carrier_runtime.snapshot("a").stage == "TURN_STARTED")
        assert prompts == ["first prompt", "pending b only"]
    else:
        wait_for(lambda: carrier._carrier_runtime.snapshot("a") is None)
        assert prompts == ["first prompt"]
    event = (
        "worker.carrier.dequeue.rerendered"
        if remaining
        else "worker.carrier.dequeue.skipped"
    )
    events = [fields for name, fields in rig.logs if name == event]
    assert len(events) == 1
    fields = events[0]
    assert fields["messageId"] == "a"
    assert fields["batchMessageIds"] == ["a", "b"]
    assert fields["remainingMessageIds"] == list(remaining)
    assert fields["enqueuedAtMs"] <= fields["dequeuedAtMs"]
    assert fields["enqueuedAtMs"] <= fields["settledAtMs"]
    assert fields["ackSource"]["observedAtMs"] == fields["settledAtMs"]
    carrier._apply_inbox_list(None, {"ok": True, "messages": []})
    assert not any(
        name in {"worker.turn.failed", "worker.turn.abandoned"} for name, _ in rig.logs
    )
    assert not any(
        fields.get("stage", "").startswith("turn-result") for _, fields in rig.logs
    )
    assert not any(method == "message.ack" for method, _ in rig.calls)


@pytest.mark.parametrize("limit", [16384, 1], ids=["inline", "spill"])
@pytest.mark.parametrize("settle_rest", [False, True], ids=["partial", "empty-race"])
def test_reopened_inbox_cursor_is_replayed_by_daemon_not_locally(
    rig, tmp_path, limit, settle_rest, request
):
    from dataclasses import replace
    from pathlib import Path

    from hyprial.daemon.impl.inbox import (
        DeliveryLifecycle,
        InboxMessage,
        InboxService,
        MemoryDeliveryTransport,
    )
    from hyprial.daemon.impl.inbox.batch.service import capture_batch
    from hyprial.daemon.impl.dispatch.admission import LinkAdmission
    from tests.daemon.application.transport.channel.support import FakeLinkGate

    admission = LinkAdmission()
    admission.bind(FakeLinkGate())
    path = tmp_path / "cursor-inbox.sqlite3"
    private = tmp_path / "actor-state"
    private.mkdir(mode=0o700)
    inbox = InboxService(path, MemoryDeliveryTransport(), link_admission=admission)
    request.addfinalizer(inbox.close)
    for message_id in ("a", "b"):
        inbox.receive(
            InboxMessage(
                message_id=message_id,
                conversation_id="replay",
                sender="agent:sender:node:s",
                recipient=ACTOR,
                payload=b"{}",
                intent="request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                idempotency_key=None,
                created_at_ms=0,
            )
        )
    original = capture_batch(
        inbox, ACTOR, keys=(ACTOR,), actor_home=private, limit_bytes=limit
    )
    assert original.message_ids == ("a", "b")
    item = replace(delivery(), message=original.text, batch_cursor=original.cursor)
    assert inbox.ack(ACTOR, "a").acknowledged
    inbox.close()
    inbox = InboxService(path, MemoryDeliveryTransport(), link_admission=admission)
    request.addfinalizer(inbox.close)
    responses = []
    try:
        carrier = rig.create(item)

        def replay_request(method, params):
            assert params["actor"] == ACTOR and params["sessionRef"] == SESSION
            if method == "message.settle_hint":
                pending = any(
                    m.message_id == params["messageId"]
                    for m in inbox.pending_messages(ACTOR)
                )
                return {"ok": True, "pending": pending}
            assert method == "message.pending.batch"
            assert params["cursor"] == original.cursor
            if settle_rest:
                assert inbox.ack(ACTOR, "b").acknowledged
            response = capture_batch(
                inbox,
                ACTOR,
                keys=(ACTOR,),
                cursor_token=params["cursor"],
                actor_home=private,
                limit_bytes=limit,
            ).response
            responses.append(response)
            return response

        carrier.daemon_request = replay_request
        wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
        carrier._drain_carrier_effects()
        assert len(responses) == 1
        if settle_rest:
            wait_for(lambda: carrier._carrier_runtime.snapshot("a") is None)
            assert carrier._pump.enqueued == []
        else:
            injected = carrier._pump.enqueued[0]
            block = responses[0]["delivery"]
            assert injected.model_prompt() == block["text"]
            assert [row["messageId"] for row in responses[0]["items"]] == ["b"]
            rendered = (
                Path(block["spillPath"]).read_text() if limit == 1 else block["text"]
            )
            assert "messageId: a\n" not in rendered
            assert "messageId: b\n" in rendered
            assert [m.message_id for m in inbox.pending_messages(ACTOR)] == ["b"]
    finally:
        inbox.close()
