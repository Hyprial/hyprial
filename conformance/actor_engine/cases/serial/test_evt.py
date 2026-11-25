# Decision record (file header; no inline comments by repository rule):
# - EVT-01 drives one actor through every event kind of section 11 and asserts
#   the per-actor kind sequence exactly, including CL-1 (the budget-crossing
#   failure emits child_failed, then child_quarantined, no restart_scheduled)
#   and the 5.1 row 6 reset pair (restart_scheduled delay 0, then
#   child_restarted). Steps are separated by bounded waits so the sequence is
#   causal, never a race.
# - Fields are asserted where section 11 pins them: tuple arity 8, actor_id,
#   name, generation (5.1), command_type = type(command).__name__ when a
#   command is associated and None for restart_scheduled / child_restarted /
#   child_stopped (no command associated, "extra fields" column), code /
#   detail verbatim for command_rejected, code = exception type name for
#   child_failed, restart_delay = the exact nominal value at jitter 0 (6.3.3).
#   The text of child_failed / child_quarantined `detail`, and the code and
#   command_type of child_quarantined, are not pinned by section 11, so they
#   are not asserted.
# - "command_completed after return, never before" is observed by the command
#   itself: while it runs, no completion event for the actor may exist yet.
# - EVT-02 needs its own engine-level sink to observe the routing ORDER (spec
#   sink first, then engine sink), so it builds a raw Engine from the backend
#   module and shuts it down itself; native factories come from the same
#   engine's native_handler_factory.
# - EVT-03 raises from the engine sink, from a spec sink and from a dead-letter
#   sink; each must change nothing: no supervision effect, the other sink
#   still receives every event, every dead letter is still offered.
# - GIL-01 (12) requires every callback to run holding no engine-internal
#   lock. Section 14 has no GIL-01 row although section 2 promises one per
#   behavioural clause (missing ID reported on PR #2251), so the sink
#   re-entry case is tagged EVT-02: the engine sink and the spec sink call
#   snapshot and tell, the dead-letter sink calls snapshot, and every such call
#   must return (a tuple, an int or a caught KeyError), never hang or raise.
"""EVT: events and sink routing (spec 11, 7, CL-1)."""

from __future__ import annotations

import threading
import time

import pytest

from backends import TRAMPOLINE, trampoline
from support.helpers import (POLICIES, WAIT, Block, Call, DeadLetters, Expected, Raise, Rec, Recorder,
                             case, wait_until)

KINDS = pytest.mark.parametrize("kind", ["python", "native"], ids=["py-handler", "native-handler"])
NO_COMMAND_KINDS = ("restart_scheduled", "child_restarted", "child_stopped")


class RaisingDeadLetters(DeadLetters):
    """Dead-letter sink that records each offer and then raises."""

    def sink(self, message: object, reason: str) -> None:
        super().sink(message, reason)
        raise RuntimeError("dead-letter sink failure")


def _raw_engine(module, sink):
    return module.Engine(policies=dict(POLICIES), jitter_seed=0, event_sink=sink,
                         expected_error_type=Expected)


def _factory(engine, kind: str):
    if kind == "native":
        return engine.native_handler_factory(TRAMPOLINE)
    return lambda: trampoline


@case("EVT-01")
def test_evt_01_fields_and_transition_order(h) -> None:
    log = Recorder()
    seen_at_run = Recorder()
    actor = h.spawn(profile="single")

    def first() -> str:
        seen_at_run.append(h.events.kinds(actor).count("command_completed"))
        return "first"

    assert h.tell(actor, Call(first)) == 0
    assert h.events.wait_kind(actor, "command_completed")
    assert seen_at_run.items() == [0]
    assert h.tell(actor, Raise(Expected("E_EVT", "evt detail"))) == 0
    assert h.events.wait_kind(actor, "command_rejected")
    assert h.tell(actor, Raise(RuntimeError("first failure"))) == 0
    assert h.events.wait_kind(actor, "child_restarted")
    assert h.tell(actor, Rec(log, "gen2")) == 0
    assert h.events.wait_kind(actor, "command_completed", 2)
    assert h.tell(actor, Raise(RuntimeError("second failure"))) == 0
    assert h.events.wait_kind(actor, "child_quarantined")
    assert h.reset(actor) is True
    assert h.events.wait_kind(actor, "child_restarted", 2)
    assert h.stop(actor) is True
    assert h.events.wait_kind(actor, "child_stopped")

    events = h.events.of(actor)
    assert [e[0] for e in events] == [
        "command_completed", "command_rejected",
        "child_failed", "restart_scheduled", "child_restarted",
        "command_completed",
        "child_failed", "child_quarantined",
        "restart_scheduled", "child_restarted",
        "child_stopped",
    ]
    assert all(len(e) == 8 and e[1:3] == (actor.actor_id, actor.name) for e in events)
    assert [e[3] for e in events] == [1, 1, 1, 1, 2, 2, 2, 2, 2, 3, 3]
    assert events[0][4:] == ("Call", None, None, None)
    assert events[1][4:] == ("Raise", "E_EVT", "evt detail", None)
    assert events[2][4:6] == ("Raise", "RuntimeError") and events[2][7] is None
    assert events[3][4:7] == (None, None, None)
    assert events[3][7] == pytest.approx(0.01, abs=1e-9)
    assert events[5][4:] == ("Rec", None, None, None)
    assert events[6][4:6] == ("Raise", "RuntimeError") and events[6][7] is None
    assert events[7][7] is None
    assert events[8][4:7] == (None, None, None)
    assert events[8][7] == pytest.approx(0.0, abs=1e-9)
    for event in (events[4], events[9], events[10]):
        assert event[4:] == (None, None, None, None)
    assert log.items() == ["gen2"]


@case("EVT-01")
def test_evt_01_restart_factory_failure_order(hp) -> None:
    calls = Recorder()

    def factory():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("factory failed at restart")
        return trampoline

    actor = hp.spawn(profile="fast", factory=factory)
    assert hp.tell(actor, Raise(RuntimeError("handler failure"))) == 0
    assert hp.events.wait_kind(actor, "child_restarted")
    events = hp.events.of(actor)
    assert [e[0] for e in events] == ["child_failed", "restart_scheduled", "child_failed",
                                      "restart_scheduled", "child_restarted"]
    assert [e[3] for e in events] == [1, 1, 1, 1, 2]
    assert events[0][4:6] == ("Raise", "RuntimeError")
    assert events[2][4:6] == (None, "RuntimeError")
    assert events[1][7] == pytest.approx(0.02, abs=1e-9)
    assert events[3][7] == pytest.approx(0.04, abs=1e-9)
    assert len(calls) == 3


@case("EVT-01")
def test_evt_01_kinds_never_emitted_for_plain_success(h) -> None:
    log = Recorder()
    actor = h.spawn()
    for value in range(3):
        assert h.tell(actor, Rec(log, value)) == 0
    assert h.events.wait_kind(actor, "command_completed", 3)
    assert h.wait_idle(actor)
    assert h.events.of(actor) == [("command_completed", actor.actor_id, actor.name, 1, "Rec",
                                   None, None, None)] * 3


@case("EVT-02")
@KINDS
def test_evt_02_spec_sink_then_engine_sink(backend, kind: str) -> None:
    order = Recorder()
    engine = _raw_engine(backend, lambda event: order.append(("engine", event)))
    try:
        factory = _factory(engine, kind)
        name_a, name_b = "evt-routed", "evt-plain"
        a = engine.spawn(name_a, factory, 8, "fast", None, lambda event: order.append(("spec", event)))
        b = engine.spawn(name_b, factory, 8, "fast")

        def engine_kinds(actor_id: str) -> list[str]:
            return [e[0] for src, e in order.items() if src == "engine" and e[1] == actor_id]

        steps = ((Rec(Recorder(), "a"), "command_completed"),
                 (Raise(Expected("E_RT", "routed")), "command_rejected"),
                 (Raise(RuntimeError("routed failure")), "child_restarted"))
        for command, kind_seen in steps:
            assert engine.tell(a, name_a, command) == 0
            assert wait_until(lambda kind_seen=kind_seen: kind_seen in engine_kinds(a))
        assert engine.tell(b, name_b, Rec(Recorder(), "b")) == 0
        assert wait_until(lambda: "command_completed" in engine_kinds(b))
        assert engine.stop(a, name_a, WAIT) is True
        assert wait_until(lambda: "child_stopped" in engine_kinds(a))

        routed = [(src, e) for src, e in order.items() if e[1] == a]
        assert [src for src, _ in routed] == ["spec", "engine"] * (len(routed) // 2)
        assert len(routed) % 2 == 0
        assert all(routed[i][1] == routed[i + 1][1] for i in range(0, len(routed), 2))
        assert [e[0] for src, e in routed if src == "spec"] == [
            "command_completed", "command_rejected", "child_failed", "restart_scheduled",
            "child_restarted", "child_stopped"]
        assert all(e[1] == a for src, e in order.items() if src == "spec")
        assert engine_kinds(b) == ["command_completed"]
    finally:
        assert engine.shutdown(5.0) is True


@case("EVT-02")
@KINDS
def test_evt_02_sinks_reenter_engine_without_engine_lock(backend, kind: str) -> None:
    engine_calls, spec_calls, dead_calls = Recorder(), Recorder(), Recorder()
    handles: dict[str, tuple[str, str]] = {}

    def probe(actor_id: str, name: str) -> object:
        try:
            return engine.snapshot(actor_id, name)
        except KeyError as exc:
            return exc

    def reenter(calls: Recorder, event: tuple) -> None:
        if event[1] != handles.get("actor", ("",))[0]:
            return
        other_id, other_name = handles["other"]
        calls.append((event[0], probe(event[1], event[2]), engine.tell(other_id, other_name, "ping")))

    def dead_sink(message: object, reason: str) -> None:
        actor_id, name = handles["actor"]
        dead_calls.append((message, reason, probe(actor_id, name)))

    engine = _raw_engine(backend, lambda event: reenter(engine_calls, event))
    try:
        factory = _factory(engine, kind)
        other = engine.spawn("evt-gil-other", factory, 64, "fast")
        handles["other"] = (other, "evt-gil-other")
        name = "evt-gil-actor"
        actor = engine.spawn(name, factory, 8, "fast", dead_sink, lambda event: reenter(spec_calls, event))
        handles["actor"] = (actor, name)

        def seen(kind_seen: str) -> bool:
            return all(kind_seen in [c[0] for c in calls.items()] for calls in (engine_calls, spec_calls))

        assert engine.ask(actor, name, "first", WAIT) == "first"
        assert wait_until(lambda: seen("command_completed"))
        assert engine.tell(actor, name, Raise(RuntimeError("gil failure"))) == 0
        assert wait_until(lambda: seen("child_restarted"))
        release, entered = threading.Event(), threading.Event()
        queued = Rec(Recorder(), "queued")
        try:
            assert engine.tell(actor, name, Block(release, entered)) == 0
            assert entered.wait(WAIT)
            assert engine.tell(actor, name, queued) == 0
            assert engine.stop(actor, name, 0) is False
        finally:
            release.set()
        assert wait_until(lambda: engine.snapshot(actor, name)[0] == "stopped")
        assert wait_until(lambda: seen("child_stopped"))
        assert wait_until(lambda: len(dead_calls) >= 1)
        for kind_seen in ("command_completed", "child_failed", "restart_scheduled", "child_restarted",
                          "child_stopped"):
            assert seen(kind_seen), kind_seen
        for calls in (engine_calls, spec_calls):
            for _, snap, told in calls.items():
                assert isinstance(snap, (tuple, KeyError)), snap
                assert told == 0, told
        time.sleep(0.05)
        assert len(dead_calls) == 1
        message, reason, snap = dead_calls.items()[0]
        assert message is queued and reason == "stopped"
        assert isinstance(snap, (tuple, KeyError)), snap
    finally:
        assert engine.shutdown(5.0) is True


@case("EVT-02")
def test_evt_02_spec_sink_receives_only_its_actor(h) -> None:
    spec = Recorder()
    routed = h.spawn(event_sink=spec.append)
    plain = h.spawn()
    assert h.ask(routed, "r") == "r"
    assert h.ask(plain, "p") == "p"
    assert h.events.wait_kind(routed, "command_completed")
    assert h.events.wait_kind(plain, "command_completed")
    assert spec.wait_len(1)
    assert spec.items() == h.events.of(routed)
    assert all(event[1] == routed.actor_id for event in spec.items())


@case("EVT-03")
@KINDS
def test_evt_03_engine_sink_exception_isolated(backend, kind: str) -> None:
    received = Recorder()

    def raising_sink(event: tuple) -> None:
        received.append(event)
        raise RuntimeError("engine sink failure")

    engine = _raw_engine(backend, raising_sink)
    try:
        name = "evt-isolated"
        actor = engine.spawn(name, _factory(engine, kind), 8, "fast")
        assert engine.ask(actor, name, "first", WAIT) == "first"
        assert wait_until(lambda: any(e[0] == "command_completed" for e in received.items()))
        assert wait_until(lambda: engine.snapshot(actor, name) == ("running", 1, 0, 0, 0))
        with pytest.raises(Expected):
            engine.ask(actor, name, Raise(Expected("E_ISO", "iso")), WAIT)
        assert wait_until(lambda: engine.snapshot(actor, name) == ("running", 1, 0, 0, 0))
        assert engine.tell(actor, name, Raise(RuntimeError("real failure"))) == 0
        assert wait_until(lambda: any(e[0] == "child_restarted" for e in received.items()))
        assert engine.ask(actor, name, "after", WAIT) == "after"
        assert engine.snapshot(actor, name)[:2] == ("running", 2)
        assert engine.stop(actor, name, WAIT) is True
        assert wait_until(lambda: any(e[0] == "child_stopped" for e in received.items()))
        assert [e[0] for e in received.items()] == [
            "command_completed", "command_rejected", "child_failed", "restart_scheduled",
            "child_restarted", "command_completed", "child_stopped"]
    finally:
        assert engine.shutdown(5.0) is True


@case("EVT-03")
def test_evt_03_spec_sink_exception_isolated(h) -> None:
    spec_calls = Recorder()

    def raising_spec_sink(event: tuple) -> None:
        spec_calls.append(event)
        raise ValueError("spec sink failure")

    actor = h.spawn(event_sink=raising_spec_sink)
    assert h.ask(actor, "one") == "one"
    assert h.tell(actor, Raise(RuntimeError("fail"))) == 0
    assert h.events.wait_kind(actor, "child_restarted")
    assert h.ask(actor, "two") == "two"
    assert h.events.wait_kind(actor, "command_completed", 2)
    assert h.snapshot(actor)[:2] == ("running", 2)
    assert h.events.kinds(actor) == ["command_completed", "child_failed", "restart_scheduled",
                                     "child_restarted", "command_completed"]
    assert spec_calls.items() == h.events.of(actor)


@case("EVT-03")
def test_evt_03_dead_letter_sink_exception_isolated(h) -> None:
    offered = RaisingDeadLetters()
    release = threading.Event()
    blocker = Block(release)
    queued = [Rec(Recorder(), index) for index in range(3)]
    actor = h.spawn(dead_letters=offered)
    try:
        assert h.tell(actor, blocker) == 0
        assert blocker.entered.wait(WAIT)
        assert [h.tell(actor, command) for command in queued] == [0, 0, 0]
        assert h.stop(actor, 0) is False
    finally:
        release.set()
    assert h.wait_state(actor, "stopped")
    assert h.events.wait_kind(actor, "child_stopped")
    assert offered.wait_len(3)
    assert [(message is command, reason) for (message, reason), command in zip(offered.items(), queued)] == [
        (True, "stopped")] * 3
    assert h.stop(actor) is True
