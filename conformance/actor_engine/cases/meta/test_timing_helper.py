# Decision record (file header; no inline comments by repository rule):
# - Meta cases for support.helpers.assert_timing / until_conclusive (HQ
#   review: load robustness must not weaken a timing clause). Trials are fake
#   callables returning fixed intervals and the control overshoot is
#   monkeypatched, so every case is deterministic: an early interval fails at
#   once (never retried), a systematically late engine (doubled delay) fails
#   every attempt even with some control slack, a single transient spike is
#   absorbed by best of N, and the control only credits the platform's own
#   measured overshoot. These cases test the suite, not the engine.
"""Suite integrity: the load-robust timing helper keeps the clause."""

from __future__ import annotations

import pytest

from support import helpers
from support.helpers import TOL, TimingFailure, assert_timing, until_conclusive


def trials(*values: object):
    queue = list(values)
    calls: list[object] = []

    def attempt() -> object:
        calls.append(queue[0])
        return queue.pop(0)

    return attempt, calls


@pytest.fixture
def overshoot(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Control overshoot fixed at 0; returns the nominal of every control measured."""
    measured: list[float] = []

    def control(nominal: float) -> float:
        measured.append(nominal)
        return 0.0

    monkeypatch.setattr(helpers, "control_overshoot", control)
    return measured


def test_strict_pass_needs_no_control(overshoot) -> None:
    attempt, calls = trials(0.1 + TOL)
    assert assert_timing(attempt, 0.1) == (0.1 + TOL,)
    assert calls == [0.1 + TOL] and overshoot == []


def test_early_interval_fails_immediately(overshoot) -> None:
    attempt, calls = trials(0.1 - TOL - 0.001, 0.1)
    with pytest.raises(TimingFailure, match="earlier than"):
        assert_timing(attempt, 0.1, attempts=5)
    assert len(calls) == 1


def test_doubled_delay_fails_every_attempt(overshoot) -> None:
    attempt, calls = trials(*([0.2] * 5))
    with pytest.raises(TimingFailure, match="all 5 attempts"):
        assert_timing(attempt, 0.1, attempts=5)
    assert len(calls) == 5 and overshoot == [0.1] * 5


def test_control_overshoot_is_credited_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helpers, "control_overshoot", lambda nominal: 0.03)
    attempt, _ = trials(0.1 + TOL + 0.03)
    assert assert_timing(attempt, 0.1, attempts=1) == (0.1 + TOL + 0.03,)
    attempt, _ = trials(0.1 + TOL + 0.031)
    with pytest.raises(TimingFailure):
        assert_timing(attempt, 0.1, attempts=1)


def test_transient_spike_absorbed_by_best_of_n(overshoot) -> None:
    attempt, calls = trials(0.4, 0.1)
    assert assert_timing(attempt, 0.1, attempts=5) == (0.1,)
    assert len(calls) == 2


def test_every_interval_and_its_own_nominal(overshoot) -> None:
    attempt, _ = trials([0.1, 0.2 + TOL + 0.01], [0.1, 0.2])
    assert assert_timing(attempt, [0.1, 0.2], attempts=2) == (0.1, 0.2)
    attempt, _ = trials([0.1, 0.1])
    with pytest.raises(TimingFailure, match="earlier than"):
        assert_timing(attempt, [0.1, 0.2])


def test_non_positive_nominal_has_no_lower_bound_and_strict_upper(overshoot) -> None:
    attempt, _ = trials(*([TOL + 0.01] * 3))
    with pytest.raises(TimingFailure):
        assert_timing(attempt, 0.0, attempts=3)
    assert overshoot == [0.0] * 3
    attempt, _ = trials(0.0)
    assert assert_timing(attempt, 0.0) == (0.0,)


def test_real_control_of_a_plain_wait_is_small_and_non_negative() -> None:
    assert 0.0 <= helpers.control_overshoot(0.0) < 1.0
    assert 0.0 <= helpers.control_overshoot(0.01) < 1.0


def test_until_conclusive() -> None:
    outcomes = [False, False, True]
    until_conclusive(lambda: outcomes.pop(0), attempts=5)
    assert outcomes == []
    with pytest.raises(TimingFailure, match="no conclusive trial"):
        until_conclusive(lambda: False, attempts=3)
    with pytest.raises(AssertionError, match="clause"):
        until_conclusive(lambda: (_ for _ in ()).throw(AssertionError("clause")), attempts=3)


def test_explicit_control_replaces_the_timed_wait(overshoot) -> None:
    attempt, _ = trials(0.3, 0.3)
    assert assert_timing(attempt, 0.0, tol=0.25, attempts=1, control=lambda: 0.06) == (0.3,)
    attempt, _ = trials(0.6, 0.6)
    with pytest.raises(TimingFailure):
        assert_timing(attempt, 0.0, tol=0.25, attempts=2, control=lambda: 0.01)
    assert overshoot == []
