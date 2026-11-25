# Decision record (file header; no inline comments by repository rule):
# - ADM cases drive only `tell` admission (5.2) plus the validation of 3.1/3.2
#   (E-8). Every return code is asserted exactly (0 / 1 / 2); admission never
#   raises.
# - "Not enqueued" is proven by FIFO: a later accepted marker command runs, and
#   the rejected command never ran before it (5.2 strict FIFO).
# - Stop acceptance is reached with `stop(timeout=0)` on a blocked handler:
#   5.4 step 6 makes that call return False and leaves the actor `stopping`.
# - Self-stop acceptance closes admission for the very handler that accepted
#   it (5.4 step 2: "every later tell returns 2").
# - The draining condition is reached with a cell drain that times out on a
#   blocked handler (5.5: targets REMAIN draining after a timeout).
# - restart_pending is observed with the `slow` profile (0.5 s backoff); the
#   case only asserts admission there, not the later restart (CUS-01 owns it).
# - ADM-05 asserts ValueError for every invalid field named by the case title,
#   for spawn and spawn_pool, and for an invalid policy table at construction
#   (3.1 "MUST be non-empty"; a value that is not a 5-tuple violates the
#   declared table shape). Value bounds inside a 5-tuple are not specified by
#   3.1/E-8 and are deliberately not probed.
"""ADM: admission (spec 5.2, 3.2, E-8)."""

from __future__ import annotations

import threading

import pytest

from support.helpers import POLICIES, WAIT, Block, Call, Expected, Raise, Rec, Recorder, case


def _flush(h, actor, log: Recorder, marker: object = "marker") -> None:
    """Run a marker command through ``actor``; FIFO means earlier admits ran first."""
    before = len(log)
    assert h.tell(actor, Rec(log, marker)) == 0
    assert log.wait_len(before + 1)


@case("ADM-01")
def test_adm_01_accepted_command_executes(h) -> None:
    log = Recorder()
    actor = h.spawn()
    assert h.tell(actor, Rec(log, "a")) == 0
    assert log.wait_len(1)
    assert log.items() == ["a"]


@case("ADM-01")
def test_adm_01_accepted_commands_execute_once_each(h) -> None:
    log = Recorder()
    actor = h.spawn(capacity=8)
    assert [h.tell(actor, Rec(log, value)) for value in ("a", "b", "c")] == [0, 0, 0]
    assert log.wait_len(3)
    assert h.events.wait_kind(actor, "command_completed", 3)
    assert log.items() == ["a", "b", "c"]


@case("ADM-02")
def test_adm_02_queued_full_overloaded_in_flight_not_counted(h) -> None:
    log = Recorder()
    release = threading.Event()
    blocker = Block(release)
    actor = h.spawn(capacity=2)
    try:
        assert h.tell(actor, blocker) == 0
        assert blocker.entered.wait(WAIT)
        assert h.tell(actor, Rec(log, "q1")) == 0
        assert h.tell(actor, Rec(log, "q2")) == 0
        assert h.snapshot(actor)[2:4] == (2, 1)
        assert h.tell(actor, Rec(log, "rejected")) == 1
        assert h.tell(actor, Rec(log, "rejected-again")) == 1
        assert h.snapshot(actor)[2:4] == (2, 1)
    finally:
        release.set()
    assert h.wait_idle(actor)
    assert log.items() == ["q1", "q2"]
    _flush(h, actor, log)
    assert log.items() == ["q1", "q2", "marker"]


@case("ADM-02")
def test_adm_02_capacity_one_admits_one_queued_while_handler_runs(h) -> None:
    log = Recorder()
    release = threading.Event()
    blocker = Block(release)
    actor = h.spawn(capacity=1)
    try:
        assert h.tell(actor, blocker) == 0
        assert blocker.entered.wait(WAIT)
        assert h.tell(actor, Rec(log, "queued")) == 0
        assert h.tell(actor, Rec(log, "over")) == 1
    finally:
        release.set()
    assert log.wait_len(1)
    assert h.wait_idle(actor)
    assert h.tell(actor, Rec(log, "after")) == 0
    assert log.wait_len(2)
    assert log.items() == ["queued", "after"]


@case("ADM-03")
def test_adm_03_closed_after_external_stop_acceptance(h) -> None:
    log = Recorder()
    release = threading.Event()
    blocker = Block(release)
    actor = h.spawn()
    try:
        assert h.tell(actor, blocker) == 0
        assert blocker.entered.wait(WAIT)
        assert h.stop(actor, 0) is False
        assert h.state(actor) == "stopping"
        assert h.tell(actor, Rec(log, "during-stopping")) == 2
    finally:
        release.set()
    assert h.wait_state(actor, "stopped")
    assert h.tell(actor, Rec(log, "after-stopped")) == 2
    assert log.items() == []


@case("ADM-03")
def test_adm_03_closed_after_self_stop_acceptance(h) -> None:
    log = Recorder()
    seen = Recorder()
    actor = h.spawn()

    def self_stop() -> None:
        seen.append(h.stop(actor, WAIT))
        seen.append(h.tell(actor, Rec(log, "after-self-stop")))

    assert h.tell(actor, Call(self_stop)) == 0
    assert seen.wait_len(2)
    assert seen.items() == [True, 2]
    assert h.wait_state(actor, "stopped")
    assert h.tell(actor, Rec(log, "after-stopped")) == 2
    assert log.items() == []


@case("ADM-03")
def test_adm_03_closed_when_quarantined(h) -> None:
    log = Recorder()
    actor = h.spawn(profile="never")
    assert h.tell(actor, Raise(RuntimeError("fail"))) == 0
    assert h.wait_state(actor, "quarantined")
    assert h.tell(actor, Rec(log, "q")) == 2
    assert h.tell(actor, Rec(log, "q2")) == 2
    assert h.state(actor) == "quarantined"
    assert log.items() == []


@case("ADM-03")
def test_adm_03_closed_while_draining(h) -> None:
    log = Recorder()
    release = threading.Event()
    blocker = Block(release)
    actor = h.spawn()
    try:
        assert h.tell(actor, blocker) == 0
        assert blocker.entered.wait(WAIT)
        completed, _, remaining = h.drain([actor], 0.02)
        assert completed is False
        assert remaining == [actor.name]
        assert h.tell(actor, Rec(log, "during-drain")) == 2
    finally:
        release.set()
    assert h.wait_idle(actor)
    assert h.tell(actor, Rec(log, "still-draining")) == 2
    assert log.items() == []


@case("ADM-03")
def test_adm_03_restart_pending_still_accepts(h) -> None:
    actor = h.spawn(profile="slow")
    assert h.tell(actor, Raise(RuntimeError("fail"))) == 0
    assert h.wait_state(actor, "restart_pending")
    assert h.tell(actor, Rec(Recorder(), "kept")) == 0
    snap = h.snapshot(actor)
    assert snap[0] == "restart_pending"
    assert snap[2] == 1


@case("ADM-04")
def test_adm_04_unknown_handle_tell_closed(h) -> None:
    log = Recorder()
    real = h.spawn()
    assert h.engine.tell("no-such-actor-id", "no-such-name", Rec(log, "x")) == 2
    assert h.engine.tell("", real.name, Rec(log, "x")) == 2
    assert h.engine.tell("0" * 32, real.name, Rec(log, "x")) == 2
    _flush(h, real, log)
    assert log.items() == ["marker"]


@case("ADM-04")
def test_adm_04_forged_handle_tell_closed(h) -> None:
    log = Recorder()
    first = h.spawn()
    second = h.spawn()
    assert h.engine.tell(first.actor_id, first.name + "-forged", Rec(log, "x")) == 2
    assert h.engine.tell(first.actor_id, second.name, Rec(log, "x")) == 2
    assert h.engine.tell(second.actor_id, first.name, Rec(log, "x")) == 2
    assert h.engine.tell(first.actor_id, "", Rec(log, "x")) == 2
    _flush(h, first, log, "m1")
    _flush(h, second, log, "m2")
    assert log.items() == ["m1", "m2"]


@case("ADM-04")
def test_adm_04_forged_handle_has_no_effect_on_stop_or_reset(h) -> None:
    log = Recorder()
    actor = h.spawn()
    forged = actor._replace(name=actor.name + "-forged")
    assert h.engine.stop("no-such-actor-id", actor.name, 0.1) is False
    assert h.stop(forged, 0.1) is False
    assert h.reset(forged) is False
    assert h.state(actor) == "running"
    _flush(h, actor, log)
    q = h.spawn(profile="never")
    assert h.tell(q, Raise(RuntimeError("fail"))) == 0
    assert h.wait_state(q, "quarantined")
    assert h.reset(q._replace(name=q.name + "-forged")) is False
    assert h.reset(q._replace(actor_id="no-such-actor-id")) is False
    assert h.state(q) == "quarantined"
    assert h.reset(q) is True


INVALID_SERIAL: tuple[dict, ...] = (
    {"name": ""}, {"name": "   "}, {"capacity": 0}, {"capacity": -1}, {"profile": "no-such-profile"},
)
INVALID_POOL: tuple[dict, ...] = INVALID_SERIAL + (
    {"pool_size": 0}, {"pool_size": -1},
    {"retry_interval": 0}, {"retry_interval": 0.0}, {"retry_interval": -0.01},
)


@case("ADM-05")
def test_adm_05_spawn_invalid_spec_value_error(h) -> None:
    for override in INVALID_SERIAL:
        args = {"name": "valid-name", "capacity": 4, "profile": "fast", **override}
        with pytest.raises(ValueError):
            h.engine.spawn(args["name"], h.factory(), args["capacity"], args["profile"])
    assert h.events.items() == []
    log = Recorder()
    actor = h.spawn()
    _flush(h, actor, log)


@case("ADM-05")
def test_adm_05_spawn_pool_invalid_spec_value_error(h) -> None:
    for override in INVALID_POOL:
        args = {"name": "valid-pool", "pool_size": 1, "capacity": 4, "profile": "fast",
                "retry_interval": 0.01, **override}
        with pytest.raises(ValueError):
            h.engine.spawn_pool(args["name"], h.factory(), args["pool_size"], args["capacity"],
                                args["profile"], None, None, args["retry_interval"])
    pool = h.spawn_pool()
    assert h.snapshot(pool)[:2] == ("running", 1)


@case("ADM-05")
def test_adm_05_unknown_native_factory_name_value_error(h) -> None:
    with pytest.raises(ValueError):
        h.engine.native_handler_factory("conformance.no-such-factory")


@case("ADM-05")
def test_adm_05_invalid_policy_table_at_construction(h) -> None:
    for policies in ({}, {"fast": (3, 60.0, 0.02, 0.2)}, {"fast": (3, 60.0, 0.02, 0.2, 0.0, 1.0)}):
        with pytest.raises(ValueError):
            h.module.Engine(policies=policies, jitter_seed=0, event_sink=lambda event: None,
                            expected_error_type=Expected)
    engine = h.module.Engine(policies=dict(POLICIES), jitter_seed=0, event_sink=lambda event: None,
                             expected_error_type=Expected)
    assert engine.shutdown(5.0) is True
