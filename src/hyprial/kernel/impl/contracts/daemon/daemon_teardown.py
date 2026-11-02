"""Shared timing contract for daemon teardown and its observers."""

from __future__ import annotations

# Final exit grace after ``DaemonApplication._close`` finishes. The owning
# daemon replaces its earlier broad close deadline with this shorter window.
DAEMON_EXIT_BACKSTOP_SECONDS = 15.0

# Recap persistence (unified hook consumers) is mechanism-owned and must
# finish before agent homes and inbox collaborators are closed.  This is a
# join budget, never a reason to loosen any existing teardown step.
TURN_HOOK_CLOSE_TIMEOUT_SECONDS = 5.0

# Every bounded close step is named here so shutdown waiters and observers can
# derive their windows from the same contract as the production close path.
DAEMON_CLOSE_STEP_BUDGETS = (
    ("autoupdate", 5.0),
    ("maintenance-scheduler", 5.0),
    ("service-connect", 5.0),
    ("lifecycle-manager", 5.0),
    ("lifecycle-port:agent", 5.0),
    ("lifecycle-port:session", 5.0),
    ("lifecycle-port:harness", 5.0),
    ("route-registration", 5.0),
    ("harnesses", 5.0),
    ("operator-alarms", 5.0),
    ("delivery-hooks", TURN_HOOK_CLOSE_TIMEOUT_SECONDS),
    ("hook-bus", TURN_HOOK_CLOSE_TIMEOUT_SECONDS),
    # Named consumers (biz/hooks) drain their writers here; the legacy v1
# turn-hook service is retired (unified hooks H2).
("hooks-consumers", TURN_HOOK_CLOSE_TIMEOUT_SECONDS),
    ("restore-thread", 2.0),
    ("org-binding-cache", 5.0),
    ("remote-workflow", 5.0),
)

DAEMON_CLOSE_BUDGET_SECONDS = sum(
    seconds for _name, seconds in DAEMON_CLOSE_STEP_BUDGETS
)

# Sum of the budgeted portion and final exit grace. The owning daemon also
# uses this window for a forced-exit timer before close starts. It bounds a
# stuck close; it does not promise that every unbudgeted step can finish.
TEARDOWN_BUDGETED_SECONDS = (
    DAEMON_CLOSE_BUDGET_SECONDS + DAEMON_EXIT_BACKSTOP_SECONDS
)
