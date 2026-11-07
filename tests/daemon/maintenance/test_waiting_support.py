"""The waiting helper's own negative controls.

The helper exists to make one failure mode loud: a wait that could not have
failed.  So the load-bearing tests here are the ones asserting it RAISES --
if those stop failing, the helper has quietly become the thing it replaced.
"""

from __future__ import annotations

import inspect
import threading

import pytest

from tests.support.runtime import waiting_support
from tests.support.runtime.waiting_support import Budget, VacuousWait, WaitTimeout, assert_absent_while, await_recorded, ensure_reached, wait_until

IPC = "hyprial.daemon.impl.ipc.server._IPC_CLIENT_IDLE_TIMEOUT"
DEADLINE = "hyprial.kernel.impl.contracts.daemon.lifecycle_budgets.LIFECYCLE_OPERATION_DEADLINE_SECONDS"
MARGIN = "hyprial.kernel.impl.contracts.daemon.lifecycle_budgets.LIFECYCLE_WAIT_MARGIN_SECONDS"


class _FakeClock:
    """Time that only moves when the code under test decides to wait.

    The first version of this file timed a ``threading.Timer(0.01, ...)``
    against a 0.05s budget and went red on a loaded CI runner -- this module's
    own defect class, inside its own tests.  A wait helper cannot be tested
    against the wall clock, because the thing under test IS the reading of the
    clock; the fourth form in the design note says so ("if time is an input to
    the subject, do not wait -- inject the clock") and this file had not
    applied it to itself.

    Here every run is identical: the deadline is reached after exactly
    ``budget / _POLL_SECONDS`` polls, on any machine, under any load.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr(waiting_support, "_monotonic", fake.monotonic)
    monkeypatch.setattr(waiting_support, "_sleep", fake.sleep)
    return fake


def _true_after(polls: int):
    """A condition that flips to True on the ``polls``-th call.

    Call 1 is the vacuity check, so ``polls=2`` means "false on entry, true on
    the first poll" -- expressed in polls rather than seconds, which is the
    whole point.
    """

    seen = {"n": 0}

    def condition() -> bool:
        seen["n"] += 1
        return seen["n"] >= polls

    return condition


# ── the bound comes from the product, and cannot be faked ──────────────────


def test_a_budget_is_derived_from_the_products_own_constant() -> None:
    """The bound equals the product's constant -- compared to a SECOND import.

    ⛔ Deliberately not ``== 15.0``: pinning the literal would make this file
    go red the day the product legitimately retunes its own timeout, which is
    precisely the false-red shape this module exists to remove.  Importing the
    constant here is an independent path to the same value, so the assertion
    still fails if ``from_product`` invented the number instead of reading it.
    """

    from hyprial.daemon.impl.ipc import server as application

    budget = Budget.from_product(IPC)

    assert budget.seconds == application._IPC_CLIENT_IDLE_TIMEOUT
    assert budget.origin == IPC


def test_budgets_compose_the_way_the_existing_fix_composed_them() -> None:
    """#609 used deadline + wait margin; the caller still writes no number."""

    from hyprial.kernel.impl.contracts.daemon import lifecycle_budgets

    budget = Budget.from_product(DEADLINE, MARGIN)

    assert budget.seconds == (
        lifecycle_budgets.LIFECYCLE_OPERATION_DEADLINE_SECONDS
        + lifecycle_budgets.LIFECYCLE_WAIT_MARGIN_SECONDS
    )
    assert DEADLINE in budget.origin and MARGIN in budget.origin


def test_a_renamed_constant_fails_loudly_instead_of_falling_back() -> None:
    """The failure this guards: a rename silently restoring a wall-clock bet."""

    with pytest.raises(AttributeError, match="do NOT replace it with a literal"):
        Budget.from_product("hyprial.daemon.impl.ipc.server._NO_SUCH_BUDGET")


def test_a_missing_module_fails_loudly() -> None:
    with pytest.raises(ImportError):
        Budget.from_product("hyprial.not_a_module.SOMETHING")


def test_a_non_numeric_constant_is_rejected() -> None:
    with pytest.raises(TypeError):
        Budget.from_product("hyprial.daemon.impl.ipc.server.__name__")


def test_a_budget_may_not_be_anchored_in_the_test_suite() -> None:
    """⭐ The hole this module shipped with, now closed.

    The first version checked only "the path imports and is numeric".  A
    literal parked in ``tests/`` under a constant-shaped name satisfied that
    and looked, at the call site, exactly like a product-derived bound -- and
    this file's own tests were the first thing to walk through it.  A bound
    the test suite owns is a wall-clock guess with a longer name: nothing
    ties it to the product, so it cannot drift from the product *visibly*.
    """

    with pytest.raises(ValueError, match="not under 'hyprial.'"):
        Budget.from_product('tests.daemon.maintenance.test_waiting_support.IPC')


# ── the vacuous-wait guard: this run had no discriminating power ───────────


def test_wait_until_returns_when_the_state_arrives(clock: _FakeClock) -> None:
    wait_until(_true_after(3), budget=Budget.from_product(IPC), proves="it arrived")

    assert clock.now > 0.0, "it should have polled at least once"


def test_wait_until_refuses_a_condition_that_already_holds() -> None:
    """⭐ The control that keeps ④ alive.

    A rewrite preserving only "no false red" would make every wait look like
    this one, and it must fail on every run rather than pass quietly.
    """

    already = threading.Event()
    already.set()

    with pytest.raises(VacuousWait, match="already held before waiting"):
        wait_until(already.is_set, budget=Budget.from_product(IPC), proves="it arrived")


def test_wait_until_reports_the_budget_source_when_it_expires(clock: _FakeClock) -> None:
    """On expiry the message names WHERE the bound came from, not just how long.

    A bare "timed out after 15.0s" sends the next reader looking for a number
    to raise.  Naming the constant sends them to the product instead.
    """

    never = threading.Event()

    with pytest.raises(WaitTimeout, match=IPC.replace(".", r"\.")):
        wait_until(never.is_set, budget=Budget.from_product(IPC), proves="it arrived")


def test_wait_until_rejects_a_must_time_out_budget() -> None:
    """Those two belong to opposite claims; the API keeps them apart."""

    with pytest.raises(ValueError, match="assert_absent_while"):
        wait_until(
            threading.Event().is_set,
            budget=Budget.must_time_out(0.05, why="proves absence"),
            proves="it arrived",
        )


# ── absence is asserted against an event, never against a duration ─────────


def test_absence_is_read_only_once_the_guard_engages(clock: _FakeClock) -> None:
    forbidden = threading.Event()

    assert_absent_while(
        forbidden.is_set,
        while_=_true_after(2),
        budget=Budget.from_product(IPC),
        proves="the second party stays out while the first holds",
    )


def test_a_guard_that_never_engages_is_a_failure_not_a_pass(clock: _FakeClock) -> None:
    """⭐ The other half of ④.

    "the other party never contended" and "the exclusion worked" are the same
    observation in the old ``sleep(); assert not ...`` form.  That ambiguity is
    what this raise removes.
    """

    never_engages = threading.Event()
    forbidden = threading.Event()

    with pytest.raises(WaitTimeout, match="proves nothing"):
        assert_absent_while(
            forbidden.is_set,
            while_=never_engages.is_set,
            budget=Budget.from_product(IPC),
            proves="the second party stays out",
        )


def test_the_forbidden_event_while_guarded_still_fails() -> None:
    guard = threading.Event()
    guard.set()
    forbidden = threading.Event()
    forbidden.set()

    with pytest.raises(AssertionError, match="while the guard was engaged"):
        assert_absent_while(
            forbidden.is_set,
            while_=guard.is_set,
            budget=Budget.from_product(IPC),
            proves="the second party stays out",
        )


# ── the exemption, and the escape hatch that must not exist ────────────────


def test_must_time_out_demands_a_reason() -> None:
    with pytest.raises(ValueError, match="non-empty why"):
        Budget.must_time_out(0.3, why="   ")


def test_must_time_out_marks_itself_for_the_lint_gate() -> None:
    """The gate keys on this flag, never on the prose in ``why``."""

    budget = Budget.must_time_out(0.3, why="proves the callback does not fire")

    assert budget.is_timeout_proof is True


def test_wait_until_has_no_switch_for_silencing_the_vacuous_check() -> None:
    """⛔ No ``allow_already_true``: an exemption needs a different API, not a flag.

    A flag would be reached for by the first person the check inconveniences,
    and it would turn the guard off exactly where it was working.
    """

    parameters = set(inspect.signature(wait_until).parameters)

    assert parameters == {"condition", "budget", "proves"}


def test_ensure_reached_is_not_interchangeable_with_wait_until() -> None:
    """Renaming the call must not be a way to silence the guard.

    ``ensure_reached`` takes no budget, so a call site cannot be converted by
    swapping the name: the signatures disagree, and the change is visible in
    review as the semantic change it is.
    """

    assert "budget" not in inspect.signature(ensure_reached).parameters

    already = threading.Event()
    already.set()
    ensure_reached(already.is_set, proves="it already holds")

    with pytest.raises(AssertionError, match="expected to already hold"):
        ensure_reached(threading.Event().is_set, proves="it already holds")


# ── monotonic conditions: the case wait_until gets wrong ───────────────────

APPEND_ONLY = "events.snapshot() is append-only; entries are never removed"


def test_await_recorded_accepts_a_record_that_already_arrived() -> None:
    """⭐ The whole reason this is a second function.

    ``wait_until`` would raise VacuousWait here.  For a cumulative record,
    already-true means the thing ARRIVED -- which is what the caller asserts.
    Applying the transient rule turns a correct test red whenever the machine
    publishes before the wait begins: the same wall-clock bet, re-aimed at
    scheduling instead of duration.
    """

    already = threading.Event()
    already.set()

    await_recorded(
        already.is_set,
        budget=Budget.from_product(IPC),
        proves="the event was recorded",
        monotonic_because=APPEND_ONLY,
    )


def test_the_two_primitives_disagree_on_the_same_input() -> None:
    """The pair is only useful if they actually differ -- pin that."""

    already = threading.Event()
    already.set()

    with pytest.raises(VacuousWait):
        wait_until(already.is_set, budget=Budget.from_product(IPC), proves="x")

    await_recorded(
        already.is_set,
        budget=Budget.from_product(IPC),
        proves="x",
        monotonic_because=APPEND_ONLY,
    )


def test_await_recorded_still_times_out_when_it_never_arrives(clock: _FakeClock) -> None:
    never = threading.Event()

    with pytest.raises(WaitTimeout, match="was never recorded"):
        await_recorded(
            never.is_set,
            budget=Budget.from_product(IPC),
            proves="the event was recorded",
            monotonic_because=APPEND_ONLY,
        )


def test_await_recorded_demands_a_monotonicity_claim() -> None:
    """⛔ The missing vacuity check is only legitimate if someone claims why.

    A blank claim would make this the general-purpose wait with the guard
    switched off -- which is exactly what having two functions prevents.
    """

    with pytest.raises(ValueError, match="non-empty monotonic_because"):
        await_recorded(
            threading.Event().is_set,
            budget=Budget.from_product(IPC),
            proves="x",
            monotonic_because="   ",
        )


def test_await_recorded_rejects_a_must_time_out_budget() -> None:
    with pytest.raises(ValueError, match="assert_absent_while"):
        await_recorded(
            threading.Event().is_set,
            budget=Budget.must_time_out(0.05, why="proves absence"),
            proves="x",
            monotonic_because=APPEND_ONLY,
        )


def test_wait_until_has_no_monotonic_flag() -> None:
    """⛔ Not a flag on wait_until: a flag gets flipped where the guard works.

    Two names make the choice visible in the diff and in review; a keyword
    argument makes it invisible at the call site.
    """

    assert set(inspect.signature(wait_until).parameters) == {"condition", "budget", "proves"}
    assert "monotonic_because" in inspect.signature(await_recorded).parameters
