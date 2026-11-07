"""Card 3c116ad2 T4: the shared rounds arithmetic and the CLI budget shape.

The poll backstop must be *derived*, not guessed: ``ceil(rows / width)``
rounds of one start timeout, with margin -- the same admission-round shape
F1 put in ``harness_actor.restore_settlement_budget``.  The pure module
exists so the actor and the CLI share one formula (the CLI cannot reach the
actor); these tests pin that the two really do agree, on the same inputs,
including the empty fleet.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.correlation.readiness_budget import (
    FOLLOWUP_MARGIN,
    START_ADMISSION_WIDTH_DEFAULT,
    START_TIMEOUT_SECONDS_DEFAULT,
    restore_followup_budget_seconds,
    restore_rounds,
)


class _NoLaunch:
    """Never started from: only the budget arithmetic is exercised here."""

    def start(self, spec: object) -> object:  # pragma: no cover - unreachable
        raise AssertionError("no test here starts a harness")


@pytest.mark.parametrize(
    ("rows", "width", "expected_rounds"),
    [
        (0, 1, 0),
        (0, 4, 0),
        (1, 4, 1),
        (4, 4, 1),
        (5, 4, 2),
        (7, 4, 2),
        (8, 4, 2),
        (9, 4, 3),
        (5, 2, 3),
        (1, 1, 1),
    ],
)
def test_restore_rounds_is_the_ceiling_of_rows_over_width(
    rows: int, width: int, expected_rounds: int
) -> None:
    """The F1 admission shape: one round per width-sized slice of the fleet."""

    assert restore_rounds(rows, width) == expected_rounds


@pytest.mark.parametrize(("rows", "width"), [(0, 1), (0, 4), (1, 4), (7, 4), (9, 4)])
def test_the_actor_settlement_rounds_and_the_pure_function_agree(
    rows: int, width: int
) -> None:
    """Same value on both sides of the share: the actor's settlement budget is
    ``(rounds + 1) x start_timeout`` where rounds comes from the same pure
    function the CLI budget reads -- harness_actor.py's line and this module
    cannot drift apart because they are one call."""

    start_timeout = 2.0
    actor = HarnessRuntimeActor(
        _NoLaunch(), start_timeout_seconds=start_timeout, start_max_workers=width
    )
    try:
        assert actor.restore_settlement_budget(rows) == pytest.approx(
            (restore_rounds(rows, width) + 1) * start_timeout
        )
    finally:
        actor.close_runtime(2.0)


def test_the_followup_budget_is_rounds_times_timeout_with_margin() -> None:
    """``rounds x start_timeout x 1.5`` over the default fleet knobs.

    5 rows at the default width 4 is 2 rounds; 2 x 60s x 1.5 = 180s -- and
    an empty fleet derives 0 seconds, the honest bound for nothing to
    restore (the notice side floors it to the smallest valid positive).
    """

    assert restore_followup_budget_seconds(5) == pytest.approx(180.0)
    assert restore_followup_budget_seconds(0) == 0.0
    assert restore_followup_budget_seconds(
        5, admission_width=2, start_timeout_seconds=0.2
    ) == pytest.approx(3 * 0.2 * FOLLOWUP_MARGIN)


def test_the_default_knobs_are_the_actor_constructor_defaults() -> None:
    """The CLI derivation uses the daemon's own defaults, not new numbers."""

    assert START_TIMEOUT_SECONDS_DEFAULT == 60.0
    assert START_ADMISSION_WIDTH_DEFAULT == 4
