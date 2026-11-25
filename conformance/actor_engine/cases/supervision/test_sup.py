# Decision record (file header; no inline comments by repository rule):
# - SUP cases drive supervision only through the ABI (spec 5.1, 6.1-6.4) and
#   observe it through snapshots and event tuples
#   (kind, actor_id, name, generation, command_type, code, detail, delay).
#   Event `detail` text is never asserted: spec 11 names the field, not its
#   wording. `code` is asserted as the exception type name (spec 11) except
#   for the native panic, whose Python-visible type name is backend-defined.
# - CL-1 (clarifications 2026-11-03) is binding: the failure that crosses the
#   restart budget emits child_failed (row 3 command_type, row 4b None) THEN
#   child_quarantined and no restart_scheduled. SUP-03 and SUP-06 assert this
#   sequence and their assertion messages cite CL-1.
# - The restart-budget deque is frozen at quarantine (5.1 row 5): it is pruned
#   only on a failure (6.3.1) and cleared by reset (6.4), so failures_in_window
#   of a quarantined actor never decays with time (SUP-03, SUP-04).
# - restart_pending is observed with the "slow" profile (SLOW_DELAY = 1.0 s
#   fixed delay), so a snapshot taken right after restart_scheduled is far
#   from the re-entry even on a loaded host.
# - Timing (HQ review under host load): SUP-05's per-restart delay and SUP-07's
#   reset re-entry go through support.helpers.assert_timing (fresh actor per
#   trial; the exact restart_scheduled delays stay exact). SUP-04's "two
#   failures inside the window accumulate" uses until_conclusive: a trial whose
#   two failures landed >= window - 2 * TOL apart (the test thread was
#   descheduled) proves nothing and is repeated; the quarantine is required in
#   every conclusive trial.
# - Several failures are queued at once where possible: restart_pending
#   accepts tells and keeps the mailbox (5.2, 6.3.5), so each queued failing
#   command fails the next generation without timing races.
# - SUP-05 uses custom policies through make_harness; jitter determinism is
#   asserted across two engines with the same jitter_seed and the same failure
#   history (6.3.3 "jitter_seed makes sample reproducible").
# - SUP-08: a stale failure report cannot be injected through the ABI. The
#   case asserts the observable consequence of the fence: every failure is
#   attributed to exactly the generation that executed it, the generation
#   advances by exactly one per failure and no report is applied twice.
# - SUP-09 tests contain ONLY BaseException / panic behaviour so that a
#   containment failure cannot hide behind an unrelated check; the retired
#   pykka backend was their original strict-xfail subject (spec 16).
# - SUP-10 needs factories that raise, which only Python factories can do,
#   plus the test-only native factory conformance.env_panic_factory (CL-2),
#   which panics at build() while BUILD_PANIC_ENV is "1". It gives SUP-06 and
#   SUP-10 a native FACTORY panic (E-9) at restart and at first start; the
#   variable is only ever set through monkeypatch so it is always restored.
#   Those native factory-panic cases start the first failure with a callable
#   RuntimeError command, never "panic", so they measure only the restart
#   factory (row 4b, E-9); the panic's child_failed code is backend-defined
#   (E-9, 11), checked as a non-empty str exactly as SUP-09 does.
# - A restart factory raising a BaseException (SystemExit, KeyboardInterrupt,
#   a custom base) takes row 4b like any other failure (6.2: no class of
#   exception escapes supervision); it is a SUP-09 case (also SUP-06).
"""SUP: supervision, restart budget, backoff, quarantine, reset (spec 5.1, 6)."""

from __future__ import annotations

import threading
import time

import pytest

from backends import BUILD_PANIC_ENV, ENV_PANIC_FACTORY, trampoline
from support.helpers import (PANIC_TYPE_NAME, POLICIES, SLOW_DELAY, TOL, WAIT, Block, Call, CustomBase,
                             DeadLetters, EventLog, Expected, Raise, Rec, Recorder, assert_timing, case,
                             until_conclusive, wait_until)

KIND, ACTOR, NAME, GEN, CTYPE, CODE, DETAIL, DELAY = range(8)
SHORT_WINDOW = POLICIES["short_window"][1]

def expect(h, actor, *prefix: object) -> None:
    """Wait (bounded) until the snapshot starts with ``prefix``; state and event order is not fixed."""
    assert wait_until(lambda: h.snapshot(actor)[:len(prefix)] == prefix), (h.snapshot(actor), prefix)



class FactoryBoom(RuntimeError):
    """Raised by restart factories in SUP-06."""


class Stamped(Recorder):
    """Per-actor event sink that records ``(monotonic_time, event)``."""

    def sink(self, event: tuple) -> None:
        self.append((time.monotonic(), event))

    def of_kind(self, kind: str) -> list[tuple[float, tuple]]:
        return [item for item in self.items() if item[1][KIND] == kind]


def of_kind(h, actor, kind: str) -> list[tuple]:
    return [event for event in h.events.of(actor) if event[KIND] == kind]


def boom() -> Raise:
    return Raise(RuntimeError("boom"))


@case("SUP-01")
def test_sup_01_failure_restarts_generation_bumps_at_reentry(h) -> None:
    """Row 3 keeps the failed generation during restart_pending; row 4a bumps it."""
    actor = h.spawn(profile="slow")
    expect(h, actor, "running", 1)
    assert h.tell(actor, boom()) == 0
    assert h.events.wait_kind(actor, "restart_scheduled")
    expect(h, actor, "restart_pending", 1)
    assert h.events.wait_kind(actor, "child_restarted")
    expect(h, actor, "running", 2)
    assert h.events.kinds(actor) == ["child_failed", "restart_scheduled", "child_restarted"]
    failed, scheduled, restarted = h.events.of(actor)
    assert failed[:CODE + 1] == ("child_failed", actor.actor_id, actor.name, 1, "Raise", "RuntimeError")
    assert scheduled[:GEN + 1] == ("restart_scheduled", actor.actor_id, actor.name, 1)
    assert scheduled[DELAY] == SLOW_DELAY
    assert restarted[:GEN + 1] == ("child_restarted", actor.actor_id, actor.name, 2)
    log = Recorder()
    assert h.tell(actor, Rec(log, "after")) == 0
    assert log.wait_len(1)
    assert h.events.wait_kind(actor, "command_completed")
    assert of_kind(h, actor, "command_completed")[0][GEN] == 2


@case("SUP-01")
def test_sup_01_restart_builds_fresh_handler_instance(hp) -> None:
    """Section 4: every restart calls the factory again and never reuses an instance."""
    builds = Recorder()

    def factory():
        builds.append(1)

        def handler(command):
            if isinstance(command, str) and command == "who":
                return handler
            return trampoline(command)

        return handler

    actor = hp.spawn(profile="fast", factory=factory)
    first = hp.ask(actor, "who")
    assert hp.tell(actor, boom()) == 0
    assert hp.events.wait_kind(actor, "child_restarted")
    second = hp.ask(actor, "who")
    assert first is not second
    assert len(builds) == 2
    assert wait_until(lambda: hp.generation(actor) == 2)


@case("SUP-02")
def test_sup_02_only_failed_child_restarts(h) -> None:
    failing, other_a, other_b = (h.spawn(profile="slow") for _ in range(3))
    assert h.tell(failing, boom()) == 0
    assert h.events.wait_kind(failing, "restart_scheduled")
    log = Recorder()
    for other in (other_a, other_b):
        assert h.tell(other, Rec(log, other.name)) == 0
    assert log.wait_len(2)
    expect(h, failing, "restart_pending")
    assert h.events.wait_kind(failing, "child_restarted")
    for other in (other_a, other_b):
        expect(h, other, "running", 1)
        assert h.snapshot(other)[4] == 0
        assert h.events.kinds(other) == ["command_completed"]
        assert of_kind(h, other, "command_completed")[0][GEN] == 1
    assert wait_until(lambda: h.generation(failing) == 2)


@case("SUP-03")
@pytest.mark.parametrize(("profile", "failures"), [("never", 1), ("single", 2)])
def test_sup_03_budget_exceeded_quarantines_generation_unchanged(h, profile, failures) -> None:
    """Row 5: the crossing failure emits child_failed then child_quarantined (CL-1)."""
    actor = h.spawn(profile=profile)
    for _ in range(failures):
        assert h.tell(actor, boom()) == 0
    assert h.events.wait_kind(actor, "child_quarantined")
    expect(h, actor, "quarantined", failures, 0, 0, failures)
    kinds = h.events.kinds(actor)
    assert kinds[-2:] == ["child_failed", "child_quarantined"], (
        f"CL-1: the budget-crossing failure must emit child_failed then child_quarantined, got {kinds}")
    assert kinds.count("restart_scheduled") == failures - 1, (
        f"CL-1: no restart_scheduled for the budget-crossing failure, got {kinds}")
    last_failed, quarantined = h.events.of(actor)[-2:]
    assert last_failed[GEN] == failures and last_failed[CTYPE] == "Raise", (
        f"CL-1: child_failed keeps row-3 command_type and the failed generation, got {last_failed}")
    assert last_failed[CODE] == "RuntimeError", f"CL-1: child_failed code is the exception type, got {last_failed}"
    assert quarantined[:GEN + 1] == ("child_quarantined", actor.actor_id, actor.name, failures)
    time.sleep(0.2)
    expect(h, actor, "quarantined", failures)
    assert h.events.kinds(actor) == kinds


@case("SUP-04")
def test_sup_04_window_prunes_old_failures(h) -> None:
    """short_window: 1 restart per 0.4 s; a failure older than the window stops counting."""
    stamps = Stamped()
    actor = h.spawn(profile="short_window", event_sink=stamps.sink)
    assert h.tell(actor, boom()) == 0
    assert h.events.wait_kind(actor, "child_restarted")
    first_failure = stamps.of_kind("child_failed")[0][0]
    time.sleep(max(0.0, first_failure + 0.4 + 0.1 - time.monotonic()))
    assert h.tell(actor, boom()) == 0
    assert h.events.wait_kind(actor, "child_restarted", count=2)
    assert "child_quarantined" not in h.events.kinds(actor)
    expect(h, actor, "running", 3, 0, 0, 1)
    delays = [event[DELAY] for event in of_kind(h, actor, "restart_scheduled")]
    assert delays == [0.01, 0.01]


@case("SUP-04")
@case("SUP-03")
def test_sup_04_failures_inside_window_accumulate(h) -> None:
    def trial() -> bool:
        stamps = Stamped()
        actor = h.spawn(profile="short_window", event_sink=stamps.sink)
        assert h.tell(actor, boom()) == 0
        assert h.tell(actor, boom()) == 0
        assert wait_until(lambda: len(stamps.of_kind("child_failed")) >= 2)
        first, second = (stamp for stamp, _ in stamps.of_kind("child_failed")[:2])
        if second - first >= SHORT_WINDOW - 2 * TOL:
            return False
        assert h.events.wait_kind(actor, "child_quarantined")
        expect(h, actor, "quarantined", 2)
        time.sleep(SHORT_WINDOW + 0.2)
        assert h.snapshot(actor)[:5] == ("quarantined", 2, 0, 0, 2)
        return True

    until_conclusive(trial, what="two failures inside the 0.4 s window")


BACKOFF = (4, 60.0, 0.1, 0.25, 0.0)
JITTER = (6, 60.0, 0.01, 0.08, 0.5)


@case("SUP-05")
@pytest.mark.parametrize("kind", ["python", "native"], ids=["py-handler", "native-handler"])
def test_sup_05_backoff_exact_at_zero_jitter_and_capped(make_harness, kind) -> None:
    """nominal = min(max, base * 2^(attempt-1)); jitter 0 -> delay == nominal exactly."""
    expected = [min(0.25, 0.1 * 2 ** attempt) for attempt in range(4)]
    assert expected == [0.1, 0.2, 0.25, 0.25]

    def attempt() -> list[float]:
        h = make_harness(kind=kind, policies={**POLICIES, "backoff": BACKOFF})
        stamps = Stamped()
        actor = h.spawn(profile="backoff", event_sink=stamps.sink)
        for _ in range(4):
            assert h.tell(actor, boom()) == 0
        assert h.events.wait_kind(actor, "child_restarted", count=4)
        scheduled = of_kind(h, actor, "restart_scheduled")
        assert [event[DELAY] for event in scheduled] == expected
        assert [event[GEN] for event in scheduled] == [1, 2, 3, 4]
        assert [event[GEN] for event in of_kind(h, actor, "child_restarted")] == [2, 3, 4, 5]
        failed_at = [stamp for stamp, _ in stamps.of_kind("child_failed")]
        restarted_at = [stamp for stamp, _ in stamps.of_kind("child_restarted")]
        assert len(failed_at) == len(restarted_at) == len(expected)
        return [end - start for start, end in zip(failed_at, restarted_at)]

    assert_timing(attempt, expected, what="restart delay per attempt (failure -> re-entry)")


def _jitter_delays(make_harness, seed: int) -> list[float]:
    h = make_harness(policies={**POLICIES, "jitter": JITTER}, jitter_seed=seed)
    actor = h.spawn(profile="jitter")
    for _ in range(6):
        assert h.tell(actor, boom()) == 0
    assert h.events.wait_kind(actor, "child_restarted", count=6)
    return [event[DELAY] for event in of_kind(h, actor, "restart_scheduled")]


@case("SUP-05")
def test_sup_05_jitter_bounds_and_seed_determinism(make_harness) -> None:
    """offset = nominal * ratio * (2*sample - 1), sample in [0, 1); equal seeds reproduce."""
    first = _jitter_delays(make_harness, 1234)
    second = _jitter_delays(make_harness, 1234)
    nominal = [min(0.08, 0.01 * 2 ** attempt) for attempt in range(6)]
    assert len(first) == 6
    for delay, base in zip(first, nominal):
        assert base * 0.5 - 1e-12 <= delay <= base * 1.5 + 1e-12, (delay, base)
    assert first == second


@case("SUP-06")
def test_sup_06_restart_factory_failure_stays_restart_pending(make_harness) -> None:
    """Row 4b: budget +1 at the failed generation, next backoff, no running actor without handler."""
    h = make_harness(policies={**POLICIES, "factory": (3, 60.0, 0.1, 1.0, 0.0)})
    builds = Recorder()

    def factory():
        builds.append(1)
        if 2 <= len(builds) <= 3:
            raise Expected("E_FACTORY", "factory refused")
        return trampoline

    actor = h.spawn(profile="factory", factory=factory)
    assert h.tell(actor, boom()) == 0
    assert h.events.wait_kind(actor, "child_failed", count=2)
    expect(h, actor, "restart_pending", 1)
    assert h.snapshot(actor)[4] == 2
    ran_with_builds = Recorder()
    assert h.tell(actor, Call(lambda: ran_with_builds.append(len(builds)))) == 0
    samples: list[tuple] = []
    restarted = wait_until(lambda: samples.append(h.snapshot(actor)) or
                           "child_restarted" in h.events.kinds(actor), WAIT, 0.005)
    assert restarted
    assert all(sample[1] == 2 for sample in samples if sample[0] == "running"), samples
    assert ran_with_builds.wait_len(1)
    assert ran_with_builds.items() == [4]
    assert h.events.kinds(actor)[:7] == ["child_failed", "restart_scheduled", "child_failed",
                                         "restart_scheduled", "child_failed", "restart_scheduled",
                                         "child_restarted"]
    failed = of_kind(h, actor, "child_failed")
    assert [(e[GEN], e[CTYPE], e[CODE]) for e in failed] == [
        (1, "Raise", "RuntimeError"), (1, None, "Expected"), (1, None, "Expected")]
    assert [e[DELAY] for e in of_kind(h, actor, "restart_scheduled")] == [0.1, 0.2, 0.4]
    assert [e[GEN] for e in of_kind(h, actor, "restart_scheduled")] == [1, 1, 1]
    assert "command_rejected" not in h.events.kinds(actor)
    assert h.wait_idle(actor)
    expect(h, actor, "running", 2, 0, 0, 3)


@case("SUP-06")
def test_sup_06_restart_factory_failure_exhausts_budget_cl1(hp) -> None:
    """CL-1: the factory failure crossing the budget emits child_failed(None) then child_quarantined."""
    builds = Recorder()

    def factory():
        builds.append(1)
        if len(builds) > 1:
            raise FactoryBoom("restart factory failed")
        return trampoline

    dead = DeadLetters()
    log = Recorder()
    release = threading.Event()
    block = Block(release)
    actor = hp.spawn(profile="single", factory=factory, dead_letters=dead)
    assert hp.tell(actor, block) == 0
    assert block.entered.wait(WAIT)
    assert hp.tell(actor, boom()) == 0
    queued = Rec(log, "never")
    assert hp.tell(actor, queued) == 0
    release.set()
    assert hp.events.wait_kind(actor, "child_quarantined")
    kinds = hp.events.kinds(actor)
    assert kinds == ["command_completed", "child_failed", "restart_scheduled", "child_failed",
                     "child_quarantined"], f"CL-1: expected child_failed then child_quarantined, got {kinds}"
    last_failed = of_kind(hp, actor, "child_failed")[-1]
    assert (last_failed[GEN], last_failed[CTYPE], last_failed[CODE]) == (1, None, "FactoryBoom"), (
        f"CL-1: row-4b child_failed carries command_type None and the failed generation, got {last_failed}")
    expect(hp, actor, "quarantined", 1, 0, 0, 2)
    assert dead.wait_len(1)
    assert dead.items()[0][0] is queued and dead.items()[0][1] == "quarantined"
    time.sleep(0.15)
    assert log.items() == []
    assert len(builds) == 2
    assert hp.events.kinds(actor) == kinds


ENV_RESTART = (3, 60.0, 0.2, 0.2, 0.0)


@case("SUP-06")
def test_sup_06_native_factory_panic_at_restart_is_row_4b(make_harness, monkeypatch) -> None:
    """E-9 + row 4b: a restart-time factory panic is a supervised failure of the failed generation."""
    h = make_harness(kind="native", policies={**POLICIES, "env_restart": ENV_RESTART})
    factory = h.engine.native_handler_factory(ENV_PANIC_FACTORY)
    actor = h.spawn(profile="env_restart", factory=factory)
    bystander = h.spawn(profile="env_restart")
    monkeypatch.setenv(BUILD_PANIC_ENV, "1")
    assert h.tell(actor, boom()) == 0
    assert h.events.wait_kind(actor, "child_failed", count=2)
    monkeypatch.delenv(BUILD_PANIC_ENV)
    expect(h, actor, "restart_pending", 1)
    assert h.snapshot(actor)[4] == 2
    samples: list[tuple] = []
    assert wait_until(lambda: samples.append(h.snapshot(actor)) or
                      "child_restarted" in h.events.kinds(actor), WAIT, 0.005)
    assert all(sample[1] == 2 for sample in samples if sample[0] == "running"), samples
    assert h.events.kinds(actor) == ["child_failed", "restart_scheduled", "child_failed",
                                     "restart_scheduled", "child_restarted"]
    failed = of_kind(h, actor, "child_failed")
    assert failed[0][GEN:CODE + 1] == (1, "Raise", "RuntimeError")
    assert (failed[1][GEN], failed[1][CTYPE]) == (1, None)
    assert failed[1][CODE] == PANIC_TYPE_NAME
    assert [e[GEN] for e in of_kind(h, actor, "restart_scheduled")] == [1, 1]
    log = Recorder()
    assert h.tell(actor, Rec(log, "after")) == 0
    assert h.tell(bystander, Rec(log, "bystander")) == 0
    assert log.wait_len(2)
    assert h.wait_idle(actor)
    expect(h, actor, "running", 2, 0, 0, 2)
    expect(h, bystander, "running", 1)
    assert "child_failed" not in h.events.kinds(bystander)


@case("SUP-06")
def test_sup_06_native_factory_panic_exhausts_budget_cl1(make_harness, monkeypatch) -> None:
    """CL-1: a restart-time factory panic crossing the budget: child_failed(None) then child_quarantined."""
    h = make_harness(kind="native")
    factory = h.engine.native_handler_factory(ENV_PANIC_FACTORY)
    dead = DeadLetters()
    actor = h.spawn(profile="single", factory=factory, dead_letters=dead)
    bystander = h.spawn(profile="single")
    monkeypatch.setenv(BUILD_PANIC_ENV, "1")
    assert h.tell(actor, boom()) == 0
    assert h.events.wait_kind(actor, "child_quarantined")
    log = Recorder()
    assert h.tell(bystander, Rec(log, "bystander")) == 0
    assert log.wait_len(1)
    monkeypatch.delenv(BUILD_PANIC_ENV)
    kinds = h.events.kinds(actor)
    assert kinds == ["child_failed", "restart_scheduled", "child_failed", "child_quarantined"], (
        f"CL-1: expected child_failed then child_quarantined and no further restart_scheduled, got {kinds}")
    failed = of_kind(h, actor, "child_failed")
    assert failed[0][GEN:CODE + 1] == (1, "Raise", "RuntimeError")
    last_failed = failed[-1]
    assert (last_failed[GEN], last_failed[CTYPE]) == (1, None), (
        f"CL-1: row-4b child_failed carries command_type None, got {last_failed}")
    assert last_failed[CODE] == PANIC_TYPE_NAME, (
        f"CL-1: row-4b child_failed carries the panic's type name (3.3/9.3), got {last_failed}")
    expect(h, actor, "quarantined", 1, 0, 0, 2)
    expect(h, bystander, "running", 1)
    time.sleep(0.15)
    assert h.events.kinds(actor) == kinds
    assert dead.items() == []


@case("SUP-07")
def test_sup_07_reset_restarts_at_delay_zero_generation_bump(h) -> None:
    box: list = []

    def attempt() -> float:
        stamps = Stamped()
        actor = h.spawn(profile="single", event_sink=stamps.sink)
        assert h.tell(actor, boom()) == 0
        assert h.tell(actor, boom()) == 0
        assert h.events.wait_kind(actor, "child_quarantined")
        expect(h, actor, "quarantined", 2)
        box[:] = [actor, len(h.events.of(actor))]
        started = time.monotonic()
        assert h.reset(actor) is True
        assert h.snapshot(actor)[:2] in (("restart_pending", 2), ("running", 3))
        assert h.events.wait_kind(actor, "child_restarted", count=2)
        return stamps.of_kind("child_restarted")[-1][0] - started

    assert_timing(attempt, 0.0, what="reset -> child_restarted at delay 0")
    actor, seen = box
    after = h.events.of(actor)[seen:]
    assert [event[KIND] for event in after] == ["restart_scheduled", "child_restarted"]
    assert after[0][GEN] == 2 and after[0][DELAY] == 0.0
    assert after[1][GEN] == 3
    expect(h, actor, "running", 3, 0, 0, 0)
    assert h.reset(actor) is False
    assert h.tell(actor, boom()) == 0
    assert h.events.wait_kind(actor, "child_restarted", count=3)
    assert h.events.kinds(actor).count("child_quarantined") == 1
    expect(h, actor, "running", 4)


@case("SUP-07")
def test_sup_07_reset_refused_while_draining(h) -> None:
    quarantined = h.spawn(profile="never")
    busy = h.spawn()
    assert h.tell(quarantined, boom()) == 0
    assert h.events.wait_kind(quarantined, "child_quarantined")
    release = threading.Event()
    block = Block(release)
    assert h.tell(busy, block) == 0
    assert block.entered.wait(WAIT)
    completed, _, _ = h.drain([quarantined, busy], 0.05)
    assert completed is False
    assert h.reset(quarantined) is False
    release.set()
    assert h.drain([quarantined, busy], WAIT)[0] is True


@case("SUP-08")
def test_sup_08_each_failure_fenced_to_its_generation(h) -> None:
    actor = h.spawn(profile="fast")
    log = Recorder()
    for _ in range(3):
        assert h.tell(actor, boom()) == 0
    assert h.tell(actor, Rec(log, "survivor")) == 0
    assert h.events.wait_kind(actor, "child_restarted", count=3)
    assert log.wait_len(1)
    assert [e[GEN] for e in of_kind(h, actor, "child_failed")] == [1, 2, 3]
    assert [e[GEN] for e in of_kind(h, actor, "restart_scheduled")] == [1, 2, 3]
    assert [e[GEN] for e in of_kind(h, actor, "child_restarted")] == [2, 3, 4]
    assert h.events.wait_kind(actor, "command_completed")
    assert of_kind(h, actor, "command_completed")[0][GEN] == 4
    kinds = h.events.kinds(actor)
    time.sleep(0.3)
    assert h.events.kinds(actor) == kinds
    expect(h, actor, "running", 4, 0, 0, 3)


@case("SUP-09")
@pytest.mark.parametrize("exc_type", [SystemExit, KeyboardInterrupt, CustomBase],
                         ids=["SystemExit", "KeyboardInterrupt", "CustomBase"])
def test_sup_09_base_exception_is_child_failure(h, exc_type) -> None:
    actor = h.spawn(profile="fast")
    log = Recorder()
    assert h.tell(actor, Raise(exc_type("base"))) == 0
    assert h.tell(actor, Rec(log, "after")) == 0
    assert h.events.wait_kind(actor, "child_restarted")
    assert log.wait_len(1)
    failed = of_kind(h, actor, "child_failed")
    assert [(e[GEN], e[CTYPE], e[CODE]) for e in failed] == [(1, "Raise", exc_type.__name__)]
    assert h.events.kinds(actor)[:3] == ["child_failed", "restart_scheduled", "child_restarted"]
    assert h.wait_idle(actor)
    expect(h, actor, "running", 2, 0, 0, 1)


BASE_RESTART = (3, 60.0, 0.05, 1.0, 0.0)


@case("SUP-09")
@case("SUP-06")
@pytest.mark.parametrize("exc_type", [SystemExit, KeyboardInterrupt, CustomBase],
                         ids=["SystemExit", "KeyboardInterrupt", "CustomBase"])
def test_sup_09_restart_factory_base_exception_is_row_4b(make_harness, exc_type) -> None:
    """6.2 + row 4b: a BaseException from the restart factory is a supervised child failure."""
    h = make_harness(policies={**POLICIES, "base_restart": BASE_RESTART})
    builds = Recorder()

    def factory():
        builds.append(1)
        if len(builds) == 2:
            raise exc_type("restart factory base")
        return trampoline

    actor = h.spawn(profile="base_restart", factory=factory)
    assert h.tell(actor, boom()) == 0
    assert h.events.wait_kind(actor, "child_restarted")
    failed = of_kind(h, actor, "child_failed")
    assert [(e[GEN], e[CTYPE], e[CODE]) for e in failed] == [
        (1, "Raise", "RuntimeError"), (1, None, exc_type.__name__)]
    assert [e[GEN] for e in of_kind(h, actor, "restart_scheduled")] == [1, 1]
    assert [e[GEN] for e in of_kind(h, actor, "child_restarted")] == [2]
    assert h.wait_idle(actor)
    expect(h, actor, "running", 2, 0, 0, 2)


@case("SUP-09")
def test_sup_09_native_panic_is_child_failure(make_harness) -> None:
    """E-9: a panic in a native handler is caught at the FFI boundary and supervised."""
    h = make_harness(kind="native")
    actor = h.spawn(profile="fast")
    log = Recorder()
    assert h.tell(actor, "panic") == 0
    assert h.tell(actor, Rec(log, "after")) == 0
    assert h.events.wait_kind(actor, "child_restarted")
    assert log.wait_len(1)
    failed = of_kind(h, actor, "child_failed")
    assert [(e[GEN], e[CTYPE]) for e in failed] == [(1, "str")]
    assert failed[0][CODE] == PANIC_TYPE_NAME
    assert h.events.kinds(actor)[:3] == ["child_failed", "restart_scheduled", "child_restarted"]
    assert h.wait_idle(actor)
    expect(h, actor, "running", 2, 0, 0, 1)


@case("SUP-10")
@pytest.mark.parametrize("exc_type", [RuntimeError, Expected, CustomBase],
                         ids=["RuntimeError", "expected-type", "CustomBase"])
def test_sup_10_first_start_factory_failure_raises_spawn_error(hp, exc_type) -> None:
    cause = exc_type("factory boom")

    def factory():
        raise cause

    dead = DeadLetters()
    spec_events = EventLog()
    name = f"sup10-{exc_type.__name__}"
    with pytest.raises(hp.module.ActorSpawnError) as info:
        hp.engine.spawn(name, factory, 4, "fast", dead.sink, spec_events.sink)
    assert info.value.__cause__ is cause
    time.sleep(0.05)
    assert hp.events.items() == []
    assert spec_events.items() == []
    assert dead.items() == []
    for guessed in ("", "0" * 32, name):
        assert hp.engine.tell(guessed, name, "x") == 2
        with pytest.raises(KeyError):
            hp.engine.snapshot(guessed, name)
    fresh = hp.spawn(name=name)
    expect(hp, fresh, "running", 1)


@case("SUP-10")
def test_sup_10_native_factory_panic_at_first_start(make_harness, monkeypatch) -> None:
    """E-9 + E-10: a native factory panic at first start fails spawn, leaves no record."""
    h = make_harness(kind="native")
    factory = h.engine.native_handler_factory(ENV_PANIC_FACTORY)
    dead = DeadLetters()
    spec_events = EventLog()
    name = "sup10-native-panic"
    monkeypatch.setenv(BUILD_PANIC_ENV, "1")
    with pytest.raises(h.module.ActorSpawnError) as info:
        h.engine.spawn(name, factory, 4, "fast", dead.sink, spec_events.sink)
    assert type(info.value.__cause__).__name__ == PANIC_TYPE_NAME
    monkeypatch.delenv(BUILD_PANIC_ENV)
    time.sleep(0.05)
    assert h.events.items() == []
    assert spec_events.items() == []
    assert dead.items() == []
    for guessed in ("", "0" * 32, name):
        assert h.engine.tell(guessed, name, "x") == 2
        with pytest.raises(KeyError):
            h.engine.snapshot(guessed, name)
    fresh = h.spawn(name=name, factory=factory)
    expect(h, fresh, "running", 1)
