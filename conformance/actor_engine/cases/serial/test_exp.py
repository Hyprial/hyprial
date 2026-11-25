# Decision record (file header; no inline comments by repository rule):
# - Expected rejections (5.1 row 2, 6.2) are run on the `never` profile
#   (max_restarts 0): if a rejection consumed restart budget, the very first
#   one would quarantine the actor, so "budget untouched" is observable both in
#   the snapshot (failures_in_window 0) and in the state.
# - "Queued messages unaffected" is shown by queueing commands behind the
#   rejecting command while a blocker holds the handler.
# - code/detail are asserted verbatim only when the exception carries them
#   (6.2 "when present", 11 "pass through verbatim"). The values for an
#   expected exception WITHOUT code/detail attributes are not pinned by the
#   spec (None vs. a derived string), so those cases assert kind/fields only.
# - EXP-02 also checks the converse of isinstance matching (3.1): with the
#   SUBCLASS configured as expected type, raising its base class is not an
#   expected rejection but a supervised child failure.
"""EXP: expected-error rejections (spec 5.1 row 2, 6.2, 3.1)."""

from __future__ import annotations

import threading

import pytest

from support.helpers import WAIT, Block, Expected, ExpectedSub, Raise, Rec, Recorder, case


def _rejected(h, actor) -> list[tuple]:
    return [event for event in h.events.of(actor) if event[0] == "command_rejected"]


def _assert_survives_rejection(h, exc: Expected) -> None:
    log = Recorder()
    release = threading.Event()
    blocker = Block(release)
    actor = h.spawn(profile="never")
    try:
        assert h.tell(actor, blocker) == 0
        assert blocker.entered.wait(WAIT)
        assert h.tell(actor, Raise(exc)) == 0
        assert h.tell(actor, Rec(log, "a")) == 0
        assert h.tell(actor, Rec(log, "b")) == 0
    finally:
        release.set()
    assert log.wait_len(2)
    assert h.wait_idle(actor)
    assert log.items() == ["a", "b"]
    assert h.snapshot(actor) == ("running", 1, 0, 0, 0)
    assert _rejected(h, actor) == [("command_rejected", actor.actor_id, actor.name, 1, "Raise",
                                    exc.code, exc.detail, None)]
    assert h.events.wait_kind(actor, "command_completed", 3)
    assert h.events.kinds(actor) == ["command_completed", "command_rejected",
                                     "command_completed", "command_completed"]


@case("EXP-01")
def test_exp_01_expected_error_rejected_actor_survives(h) -> None:
    _assert_survives_rejection(h, Expected("E_REJECT", "rejected by test"))


@case("EXP-01")
def test_exp_01_repeated_rejections_never_consume_budget(h) -> None:
    log = Recorder()
    actor = h.spawn(profile="never", capacity=16)
    for index in range(5):
        assert h.tell(actor, Raise(Expected(f"E_{index}", f"detail {index}"))) == 0
    assert h.tell(actor, Rec(log, "after")) == 0
    assert log.wait_len(1)
    assert h.wait_idle(actor)
    assert h.snapshot(actor) == ("running", 1, 0, 0, 0)
    assert [(event[5], event[6]) for event in _rejected(h, actor)] == [
        (f"E_{index}", f"detail {index}") for index in range(5)]
    kinds = h.events.kinds(actor)
    assert "child_failed" not in kinds
    assert "restart_scheduled" not in kinds
    assert "child_quarantined" not in kinds


@case("EXP-01")
def test_exp_01_any_configured_expected_type_matches(make_harness) -> None:
    h = make_harness(expected=LookupError)
    log = Recorder()
    actor = h.spawn(profile="never")
    assert h.tell(actor, Raise(KeyError("missing"))) == 0
    assert h.tell(actor, Rec(log, "after")) == 0
    assert log.wait_len(1)
    assert h.wait_idle(actor)
    assert h.snapshot(actor) == ("running", 1, 0, 0, 0)
    rejected = _rejected(h, actor)
    assert len(rejected) == 1
    assert rejected[0][:5] == ("command_rejected", actor.actor_id, actor.name, 1, "Raise")
    assert rejected[0][7] is None


@case("EXP-02")
def test_exp_02_subclass_of_expected_type_behaves_the_same(h) -> None:
    _assert_survives_rejection(h, ExpectedSub("E_SUB", "subclass rejection"))


@case("EXP-02")
@pytest.mark.parametrize("kind", ["python", "native"], ids=["py-handler", "native-handler"])
def test_exp_02_base_of_configured_subclass_is_child_failure(make_harness, kind: str) -> None:
    h = make_harness(kind=kind, expected=ExpectedSub)
    log = Recorder()
    actor = h.spawn(profile="fast")
    assert h.tell(actor, Raise(ExpectedSub("E_SUB", "sub"))) == 0
    assert h.events.wait_kind(actor, "command_rejected")
    assert h.tell(actor, Raise(Expected("E_BASE", "base"))) == 0
    assert h.events.wait_kind(actor, "child_restarted")
    assert h.tell(actor, Rec(log, "after")) == 0
    assert log.wait_len(1)
    assert h.generation(actor) == 2
    failed = [event for event in h.events.of(actor) if event[0] == "child_failed"]
    assert len(failed) == 1
    assert failed[0][:6] == ("child_failed", actor.actor_id, actor.name, 1, "Raise", "Expected")
