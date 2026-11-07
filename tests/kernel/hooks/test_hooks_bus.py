"""HookBus slice 1: isolation, counted loss, per-lane order, default off.

Design notes/actor-hooks/design.md §9 tests 1, 3, 5, 8, 9.  Wedged observers
block on an Event that ``finally`` releases, so every lane can close.
"""

from __future__ import annotations

import ast
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from hyprial.kernel import (
    HOOK_EVENT_SCHEMA_VERSION,
    HookBus,
    HookEvent,
    HooksConfig,
    graph_closed_event_payload,
    turn_event_payload,
    worker_cleanup_complete_event_payload,
)


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not satisfied before timeout")


def _event(name: str = "turn.completed", seq: int = 0) -> HookEvent:
    return HookEvent(
        event=name,
        kind="observe",
        emitted_at_ms=1_000 + seq,
        seq=seq,
        source="test",
        actor="agent:owner:node:worker",
        payload={"n": seq},
    )


class _Recorder:
    def __init__(self) -> None:
        self.seen: list[int] = []
        self._lock = threading.Lock()

    def observe(self, event: HookEvent) -> None:
        with self._lock:
            self.seen.append(event.seq)

    def count(self) -> int:
        with self._lock:
            return len(self.seen)


class _Wedged:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.seen: list[int] = []

    def observe(self, event: HookEvent) -> None:
        self.entered.set()
        assert self.release.wait(10.0)
        self.seen.append(event.seq)


class _Raising:
    def __init__(self) -> None:
        self.calls = 0

    def observe(self, event: HookEvent) -> None:
        self.calls += 1
        raise RuntimeError("observer bug")


def _lane(bus: HookBus, name: str):  # type: ignore[no-untyped-def]
    return next(lane for lane in bus.projection() if lane.name == name)


def test_wedged_observer_cannot_delay_publish_and_isolates_its_lane() -> None:
    """§9 #1 and #8: a wedged observer blocks neither publish nor other lanes."""

    bus = HookBus()
    wedged, fast = _Wedged(), _Recorder()
    try:
        bus.register("wedged", wedged, ["turn.completed"], queue=4)
        bus.register("fast", fast, ["turn.completed"], queue=64)
        bus.publish(_event(seq=0))
        assert wedged.entered.wait(2.0)

        started = time.monotonic()
        for seq in range(1, 40):
            bus.publish(_event(seq=seq))
        elapsed = time.monotonic() - started

        assert elapsed < 0.5, f"publish waited on a wedged observer ({elapsed:.3f}s)"
        _wait_until(lambda: fast.count() == 40)
        assert fast.seen == list(range(40))
        stuck = _lane(bus, "wedged")
        assert stuck.overloaded > 0
        assert _lane(bus, "fast").overloaded == 0
    finally:
        wedged.release.set()
        assert bus.close(5.0)


def test_raising_observer_is_counted_and_its_lane_keeps_delivering() -> None:
    """§9 #1: a raising observer fails only its own calls."""

    bus = HookBus()
    raising = _Raising()
    try:
        bus.register("raising", raising, ["turn.failed"])
        for seq in range(5):
            assert bus.publish(_event("turn.failed", seq)) == 1
        _wait_until(lambda: _lane(bus, "raising").failed == 5)
        lane = _lane(bus, "raising")
        assert (lane.accepted, lane.delivered, lane.failed) == (5, 0, 5)
        assert raising.calls == 5
    finally:
        assert bus.close(5.0)


def test_loss_is_counted_reconciled_and_logged_on_an_interval() -> None:
    """§9 #3: every published event is accounted for; drops log once per interval."""

    logs: list[tuple[str, str, str, dict]] = []
    clock = [100.0]
    bus = HookBus(
        logger=lambda level, component, name, **fields: logs.append(
            (level, component, name, fields)
        ),
        monotonic=lambda: clock[0],
    )
    wedged = _Wedged()
    published = 200
    try:
        bus.register("slow", wedged, ["delivery.settled"], queue=2)
        bus.publish(_event("delivery.settled", 0))
        assert wedged.entered.wait(2.0)
        for seq in range(1, published):
            bus.publish(_event("delivery.settled", seq))
        _wait_until(
            lambda: _lane(bus, "slow").accepted
            + _lane(bus, "slow").overloaded
            == published
        )
        lost = _lane(bus, "slow")
        assert lost.overloaded > 0
        overflow = [entry for entry in logs if entry[2] == "hooks.lane_overflow"]
        assert len(overflow) == 1, "drops within one interval log once"
        assert overflow[0][0] == "warn" and overflow[0][3]["lane"] == "slow"

        clock[0] += 61.0
        bus.publish(_event("delivery.settled", published))
        published += 1
        _wait_until(
            lambda: _lane(bus, "slow").accepted + _lane(bus, "slow").overloaded
            == published
        )
        # The lane is still full (wedged worker holding the only slots), so
        # this publish is certainly refused and must log again now the
        # interval has passed.
        assert _lane(bus, "slow").overloaded == lost.overloaded + 1
        overflow = [entry for entry in logs if entry[2] == "hooks.lane_overflow"]
        assert len(overflow) == 2, "a drop after the interval logs again"
    finally:
        wedged.release.set()
    _wait_until(
        lambda: (lane := _lane(bus, "slow")).delivered + lane.failed == lane.accepted
    )
    final = _lane(bus, "slow")
    assert final.accepted + final.overloaded + final.closed == published
    assert final.delivered + final.failed == final.accepted
    assert final.delivered == len(wedged.seen)
    assert bus.close(5.0)


def test_events_reach_an_observer_in_publish_order() -> None:
    """§9 #5: per-lane FIFO by source seq.

    Only per-source order is promised.  There is deliberately no global order
    across sources, so no test asserts one.
    """

    class _Uneven(_Recorder):
        # Even events take longer, so any concurrent execution would let odd
        # events overtake them; only a single in-order worker keeps seq order.
        def observe(self, event: HookEvent) -> None:
            if event.seq % 2 == 0:
                time.sleep(0.002)
            super().observe(event)

    bus = HookBus()
    recorder = _Uneven()
    try:
        bus.register("ordered", recorder, ["turn.started"], queue=512)
        for seq in range(100):
            assert bus.publish(_event("turn.started", seq)) == 1
        _wait_until(lambda: recorder.count() == 100)
        assert recorder.seen == list(range(100))
    finally:
        assert bus.close(5.0)


def test_default_off_does_no_work() -> None:
    """§9 #9: no registration, or no subscriber for a name, means no work."""

    bus = HookBus()
    assert bus.projection() == ()
    assert bus.publish(_event("turn.completed")) == 0

    bus.register("only-failures", _Recorder(), ["turn.failed"])
    assert bus.publish(_event("turn.completed")) == 0
    lane = _lane(bus, "only-failures")
    assert (lane.accepted, lane.overloaded, lane.closed) == (0, 0, 0)
    assert bus.close(5.0)


def test_bus_rejects_intercepts_unknown_events_and_use_after_close() -> None:
    bus = HookBus()
    intercept = HookEvent(
        event="delivery.prepared", kind="intercept", emitted_at_ms=1, seq=0,
        source="test",
    )
    with pytest.raises(ValueError, match="observe events only"):
        bus.publish(intercept)
    with pytest.raises(ValueError, match="unknown hook events"):
        bus.register("typo", _Recorder(), ["turn.complete"])
    bus.register("dup", _Recorder(), ["turn.completed"])
    with pytest.raises(ValueError, match="already registered"):
        bus.register("dup", _Recorder(), ["turn.completed"])
    assert bus.close(5.0)
    with pytest.raises(RuntimeError, match="closed"):
        bus.register("late", _Recorder(), ["turn.completed"])
    assert bus.publish(_event()) == 0


def test_close_never_strands_accepted_events_silently() -> None:
    """Accepted events are either observed or visibly stranded, never dropped.

    A wedged observer makes close() return False, and the stranded events stay
    visible as ``accepted - delivered - failed``.  Once the observer returns, a
    second close() finishes and every accepted event has been observed.
    """

    bus = HookBus()
    wedged = _Wedged()
    bus.register("closing", wedged, ["actor.up"], queue=8)
    bus.publish(_event("actor.up", 0))
    assert wedged.entered.wait(2.0)
    for seq in range(1, 6):
        assert bus.publish(_event("actor.up", seq)) == 1
    try:
        assert bus.close(0.2) is False
        lane = _lane(bus, "closing")
        assert not lane.shut
        assert lane.accepted == 6
        assert lane.accepted - lane.delivered - lane.failed == 6
        assert bus.publish(_event("actor.up", 6)) == 0, "no admission while closing"
    finally:
        wedged.release.set()
    assert bus.close(5.0)
    lane = _lane(bus, "closing")
    assert lane.shut
    assert (lane.accepted, lane.delivered, lane.failed) == (6, 6, 0)
    assert wedged.seen == list(range(6))


def test_settlement_happens_on_the_worker_that_ran_observe(monkeypatch) -> None:
    """Behavioural guard for the drained-runtime strand (bcd78661).

    Settlement must happen on the same thread that ran ``observe()``.  If it
    were handed to anything else (an actor, another thread's queue, an
    external sink), that thing could stop on its own and strand accepted
    events.  Thread identity is compared with ``get_ident``, not names.
    """

    from hyprial.kernel.impl.hooks import bus as bus_module

    settled_on: list[int] = []
    original = bus_module._Lane._settle

    def recording(self, completed):  # type: ignore[no-untyped-def]
        settled_on.append(threading.get_ident())
        return original(self, completed)

    monkeypatch.setattr(bus_module._Lane, "_settle", recording)

    class _ThreadRecorder:
        def __init__(self) -> None:
            self.observed_on: list[int] = []

        def observe(self, event: HookEvent) -> None:
            self.observed_on.append(threading.get_ident())

    recorder = _ThreadRecorder()
    bus = HookBus()
    try:
        bus.register("threads", recorder, ["turn.completed"])
        for seq in range(10):
            assert bus.publish(_event(seq=seq)) == 1
        _wait_until(lambda: _lane(bus, "threads").delivered == 10)
        assert len(settled_on) == 10
        assert len(set(recorder.observed_on)) == 1, "one worker per lane"
        assert set(settled_on) == set(recorder.observed_on)
        assert threading.get_ident() not in settled_on
    finally:
        assert bus.close(5.0)


def test_bus_settlement_does_not_depend_on_any_actor() -> None:
    """Regression guard for the drained-runtime strand (bcd78661).

    There, each lane's completions were settled by an owner actor.  Draining
    that actor's runtime stranded accepted events uncounted, and close() could
    never finish.  The bus now settles on its own effect worker, so no
    ActorRuntime/ActorSpec may enter the hooks package.
    """

    package = Path(__file__).resolve().parents[3] / 'src/hyprial' / "hooks"
    forbidden = {"ActorRuntime", "ActorSpec", "ActorHandle"}
    for source in package.glob("*.py"):
        tree = ast.parse(source.read_text())
        names = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert not names & forbidden, f"{source.name} imports {sorted(names & forbidden)}"


def test_hook_event_envelope_is_validated_and_frozen() -> None:
    event = _event()
    assert event.payload["n"] == 0
    with pytest.raises(TypeError):
        event.payload["n"] = 1  # type: ignore[index]
    with pytest.raises(ValueError, match="unknown hook event"):
        HookEvent(event="turn.done", kind="observe", emitted_at_ms=1, seq=0, source="t")
    with pytest.raises(ValueError, match="cannot be intercepted"):
        HookEvent(event="turn.completed", kind="intercept", emitted_at_ms=1, seq=0, source="t")
    with pytest.raises(ValueError, match="schema"):
        HookEvent(
            event="turn.completed", kind="observe", emitted_at_ms=1, seq=0,
            source="t", schema_version=HOOK_EVENT_SCHEMA_VERSION + 1,
        )


def test_turn_event_payload_is_versioned_bounded_and_redacted() -> None:
    payload = turn_event_payload(
        delivery_id="delivery-1",
        sender="agent:owner:node:sender",
        conversation="conversation-1",
        started_at_ms=10,
        ended_at_ms=20,
        status="failed",
        tool_names=("reader", "/private/tool", "reader"),
        output="token=secret " + "x" * 2_000,
        prompt="password=hunter2 /private/worktree/input.txt",
        tool_calls=(("reader", False),),
        failure_code="PROVIDER_TRANSIENT_FAILURE",
        fidelity="inferred",
    )

    assert payload == {
        "deliveryId": "delivery-1",
        "sender": "agent:owner:node:sender",
        "conversation": "conversation-1",
        "startedAtMs": 10,
        "endedAtMs": 20,
        "status": "failed",
        "toolNames": ["reader"],
        "outputExcerpt": payload["outputExcerpt"],
        "promptExcerpt": "password=<redacted> <path>",
        "toolCalls": [{"name": "reader", "ok": False}],
        "failureCode": "PROVIDER_TRANSIENT_FAILURE",
        "collectionMethod": "daemon",
        "fidelity": "inferred",
    }
    assert "secret" not in payload["outputExcerpt"]
    assert len(payload["outputExcerpt"]) <= 1_000


@pytest.mark.parametrize(
    ("secret", "sentinel"),
    [
        ("Authorization: Bearer bearer-secret-value", "bearer-secret-value"),
        ("Bearer bare-secret-value", "bare-secret-value"),
        ("sk-secret-value", "sk-secret-value"),
        ("ghp_secretvalue0123456789", "ghp_secretvalue0123456789"),
        ("github_pat_secretvalue0123456789", "github_pat_secretvalue0123456789"),
        ("xoxb-secretvalue0123456789", "xoxb-secretvalue0123456789"),
        ("AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ],
)
def test_turn_event_payload_redacts_forwarded_token_shapes(
    secret: str, sentinel: str
) -> None:
    payload = turn_event_payload(
        delivery_id="delivery-1",
        sender="sender",
        conversation="conversation",
        started_at_ms=1,
        ended_at_ms=2,
        status="completed",
        tool_names=(),
        output=f"output {secret}",
        prompt=f"prompt {secret}",
        tool_calls=(),
        fidelity="observed",
    )

    assert sentinel not in payload["promptExcerpt"]
    assert sentinel not in payload["outputExcerpt"]


HANDLER = "agent:owner:node:handler"


def test_hooks_config_parses_tier_a_registrations() -> None:
    config = HooksConfig.from_json(
        {
            "schemaVersion": 2,
            "hooks": [
                {"events": ["turn.completed", "turn.failed"], "handler": HANDLER,
                 "timeoutMs": 3000, "queue": 256, "options": {"sample": True}},
                {"events": ["delivery.prepared"], "mode": "intercept",
                 "handler": HANDLER},
            ],
        }
    )
    observe, intercept = config.hooks
    assert observe.mode == "observe"
    assert observe.events == frozenset({"turn.completed", "turn.failed"})
    assert (observe.timeout_ms, observe.queue) == (3000, 256)
    assert observe.handler == HANDLER and observe.consumer is None
    assert dict(observe.options) == {"sample": True}
    assert intercept.mode == "intercept"
    assert intercept.handler == HANDLER and intercept.consumer is None
    assert HooksConfig.from_json({"schemaVersion": 2}).hooks == ()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"schemaVersion": 1, "hooks": []}, "schemaVersion 2"),
        ({"schemaVersion": 2, "hooks": {}}, "must be a list"),
        ({"schemaVersion": 2, "hooks": [{"events": [], "handler": HANDLER}]}, "non-empty"),
        ({"schemaVersion": 2, "hooks": [{"events": ["turn.x"], "handler": HANDLER}]},
         "unknown hook events"),
        ({"schemaVersion": 2, "hooks": [{"events": ["turn.completed"], "mode": "intercept",
                                         "handler": HANDLER}]}, "can be intercepted"),
        ({"schemaVersion": 2, "hooks": [{"events": ["turn.completed"], "handler": "worker"}]},
         "canonical agent URI"),
        ({"schemaVersion": 2, "hooks": [{"events": ["turn.completed"], "handler": HANDLER,
                                         "timeoutMs": 0}]}, "timeoutMs"),
        ({"schemaVersion": 2, "hooks": [{"events": ["turn.completed"], "handler": HANDLER,
                                         "queue": True}]}, "queue"),
        ({"schemaVersion": 2, "hooks": [{"events": ["turn.completed"],
                                           "consumer": "recap", "handler": HANDLER}]},
         "exactly one"),
        ({"schemaVersion": 2, "hooks": [{"events": ["turn.completed"]}]},
         "exactly one"),
        ({"schemaVersion": 2, "hooks": [{"events": ["delivery.prepared"],
                                         "mode": "intercept", "consumer": "recap"}]},
         "intercept hooks require a handler"),
    ],
)
def test_hooks_config_rejects_invalid_registrations(value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        HooksConfig.from_json(value)


def test_publish_after_close_reaches_no_lane() -> None:
    bus = HookBus()
    bus.register("gone", _Recorder(), ["graph.closed"])
    assert bus.close(5.0)
    assert bus.publish(_event("graph.closed")) == 0
    assert _lane(bus, "gone").accepted == 0


def test_graph_closed_payload_is_versioned_bounded_and_null_safe() -> None:
    """The design §2.2 contract fixed 2026-10-08 (H5)."""

    managed = graph_closed_event_payload(
        graph_id="wf-1",
        end_state="completed",
        closed_by="finish",
        reason_ref="result:finish",
        closed_at_ms=42,
    )
    assert managed == {
        "graphId": "wf-1",
        "endState": "completed",
        "closedBy": "finish",
        "reasonRef": "result:finish",
        "closedAtMs": 42,
        "collectionMethod": "daemon",
        "fidelity": "observed",
    }

    cancelled = graph_closed_event_payload(
        graph_id="wf-2",
        end_state="cancelled",
        closed_by="user:owner",
        reason_ref=None,
        closed_at_ms=43,
    )
    assert cancelled["endState"] == "cancelled"
    assert cancelled["reasonRef"] is None, "absent reason is null, never empty"

    # An unmanaged graph closed by its end flag records no state.
    unmanaged = graph_closed_event_payload(
        graph_id="task-1",
        end_state=None,
        closed_by="end",
        reason_ref="",
        closed_at_ms=44,
    )
    assert unmanaged["endState"] is None
    assert unmanaged["reasonRef"] is None

    with pytest.raises(ValueError, match="end_state"):
        graph_closed_event_payload(
            graph_id="wf-3", end_state="done", closed_by=None,
            reason_ref=None, closed_at_ms=1,
        )


def test_worker_cleanup_complete_payload_is_graph_scoped() -> None:
    payload = worker_cleanup_complete_event_payload(
        graph_id="wf-1",
        actor_node="work",
        actor_name="worker-a",
        operation_id="pac-cleanup:abc",
        at_ms=99,
    )
    assert payload == {
        "graphId": "wf-1",
        "actorNode": "work",
        "actorName": "worker-a",
        "operationId": "pac-cleanup:abc",
        "workerState": "stopped",
        "atMs": 99,
        "collectionMethod": "daemon",
        "fidelity": "observed",
    }


def test_hook_event_publisher_stamps_one_source_fifo() -> None:
    from hyprial.kernel import HookEventPublisher

    bus = HookBus()
    recorder = _Recorder()
    try:
        bus.register("graph-facts", recorder, ["graph.closed"])
        publisher = HookEventPublisher(bus, source="daemon.pac")
        for index in range(3):
            publisher.publish(
                "graph.closed",
                payload={"n": index},
                emitted_at_ms=1_000 + index,
                graph_id=f"wf-{index}",
            )
        _wait_until(lambda: recorder.count() == 3)
        assert recorder.seen == [0, 1, 2]
    finally:
        assert bus.close(5.0)
