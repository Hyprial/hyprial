"""Restore-readiness budget arithmetic shared by the daemon and the CLI.

F1 (PR #332) derived the restore settlement bound in ``harness_actor`` from
the admission structure: every desired target either settles immediately as
deferred or is an admitted start bounded by one start timeout, so a restore
cannot outlive ``ceil(targets / width)`` rounds of one start timeout each.
Card 3c116ad2 needs the same arithmetic outside the daemon -- the CLI's
post-restart restore poll and the pre-restart notice budget -- so the rounds
formula lives here once and both callers share it.  Pure arithmetic only:
the actor imports it without the CLI and the CLI imports it lazily (the
``hyprial.daemon`` package must stay off CLI startup), which is also why no
constant here may depend on anything heavier than ``int``/``float``.
"""

from __future__ import annotations

# The two knobs F1's bound is stated in, at the values a daemon boot gets
# when nobody overrides them: the ``HarnessRuntimeActor`` constructor
# defaults.  Named here so the actor-side default and the CLI-side
# derivation cannot drift apart -- the CLI cannot reach the actor (the
# daemon may not even be up), so the shared module is the single source.
START_TIMEOUT_SECONDS_DEFAULT = 60.0
START_ADMISSION_WIDTH_DEFAULT = 4

# Margin over ``rounds x start_timeout`` for the CLI's post-restart
# follow-up poll.  Same intent as the settlement bound's ``+ 1`` margin
# round in ``harness_actor.restore_settlement_budget`` -- the bound is
# structural and the margin absorbs what the structure does not model
# (admission and scheduler jitter) -- stated as a factor here because the
# poll also spans the restart overhead between its readings.  Source: card
# 3c116ad2 ruling (2026-09-04).  A margin, not a measurement.
FOLLOWUP_MARGIN = 1.5


def restore_rounds(target_count: int, admission_width: int) -> int:
    """Admission rounds a restore of ``target_count`` targets needs.

    Identical arithmetic to the settlement bound (F1,
    ``harness_actor.restore_settlement_budget``): one round admits at most
    ``admission_width`` starts, each bounded by one start timeout, so
    ``ceil(targets / width)`` rounds is the structural lifetime of the
    batch.  ``target_count <= 0`` is 0 -- an empty batch owes no round.
    Callers guarantee ``admission_width >= 1``; both production
    constructors clamp before this is reached.
    """

    return -(-max(0, target_count) // admission_width)


def restore_followup_budget_seconds(
    target_count: int,
    *,
    admission_width: int = START_ADMISSION_WIDTH_DEFAULT,
    start_timeout_seconds: float = START_TIMEOUT_SECONDS_DEFAULT,
) -> float:
    """Wall-clock backstop for the CLI's post-restart restore poll.

    ``rounds x start_timeout x FOLLOWUP_MARGIN`` -- the F1 shape (the batch
    cannot outlive its admission rounds of one start timeout each), widened
    by the margin above.  Derived from the machine's own desired state, so
    it scales with the fleet instead of guessing at one; 0 rows means 0
    seconds, which is the honest bound for a fleet with nothing to restore.
    """

    return (
        restore_rounds(target_count, admission_width)
        * start_timeout_seconds
        * FOLLOWUP_MARGIN
    )
