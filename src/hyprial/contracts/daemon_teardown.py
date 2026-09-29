"""Shared timing contract for daemon teardown and its observers."""

from __future__ import annotations

# Grace between ``DaemonApplication._close`` finishing and the process being
# forced out.  This outlasts interpreter teardown; it does not bound close
# steps that have no timeout of their own.
DAEMON_EXIT_BACKSTOP_SECONDS = 15.0

# Recap persistence is mechanism-owned and must finish before its agent homes
# and inbox collaborators are closed.  This is a join budget, never a reason to
# loosen any existing teardown step.
TURN_HOOK_CLOSE_TIMEOUT_SECONDS = 5.0

# Every bounded close step is named here so shutdown waiters and observers can
# derive their windows from the same contract as the production close path.
DAEMON_CLOSE_STEP_BUDGETS = (
    ("autoupdate", 5.0),
    ("maintenance-scheduler", 5.0),
    ("lifecycle-manager", 5.0),
    ("lifecycle-port:agent", 5.0),
    ("lifecycle-port:session", 5.0),
    ("lifecycle-port:harness", 5.0),
    ("route-registration", 5.0),
    ("harnesses", 5.0),
    ("turn-hooks", TURN_HOOK_CLOSE_TIMEOUT_SECONDS),
    ("restore-thread", 2.0),
    ("remote-workflow", 5.0),
)

DAEMON_CLOSE_BUDGET_SECONDS = sum(
    seconds for _name, seconds in DAEMON_CLOSE_STEP_BUDGETS
)

# This is the budgeted portion plus the exit backstop, not a hard bound for
# unbudgeted close steps.  The deliberately qualified name prevents callers
# from claiming more than the contract provides.
TEARDOWN_BUDGETED_SECONDS = (
    DAEMON_CLOSE_BUDGET_SECONDS + DAEMON_EXIT_BACKSTOP_SECONDS
)
