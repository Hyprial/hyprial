"""Observation helpers for ``daemon service`` and ``daemon stop``.

Z-8 N2 (2026-11-17): the wait after a service install is the one ``daemon
start`` wait (``_wait_for_daemon``); this module only adapts the manager's
"is the unit's job running" observation to it. KP-092 option B (owner,
2026-11-17): the additive ``daemon stop`` fields for a loaded unit this
installation cannot attribute.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject, is_start_verdict
from typing import Any


JOB_EXIT_GRACE_SECONDS = 2.0
JOB_POLL_SECONDS = 0.25


def _startup_waiter(state_dir: Path) -> Callable[[Callable[[], bool]], bool]:
    """Run the one ``daemon start`` wait for a unit's daemon; the manager only
    observes whether the unit's job is running (N2, 2026-11-17).

    A job that ran and then stayed down for the exit grace is a crash; a job
    never observed running is just slow to spawn and falls under the wait's
    own no-progress budget. Returns False when the wait gives up.
    """

    from datetime import UTC, datetime

    from hyprial.shell.impl.cli.commands.common.daemon_start import (
        _daemon_startup_phase_summary,
    )
    from hyprial.shell.impl.cli.commands.common.support import (
        DAEMON_START_IDLE_BUDGET_SECONDS,
    )

    spawned_at = datetime.now(UTC)

    def wait(job_running: Callable[[], bool]) -> bool:
        services = get_services()
        seen_running = False
        down_since: float | None = None
        checked_at: float | None = None
        exited = False

        def job_exited() -> bool:
            nonlocal seen_running, down_since, checked_at, exited
            now = services.time.monotonic()
            if exited or (
                checked_at is not None and now - checked_at < JOB_POLL_SECONDS
            ):
                return exited
            checked_at = now
            if job_running():
                seen_running, down_since = True, None
            elif seen_running:
                down_since = now if down_since is None else down_since
                exited = now - down_since >= JOB_EXIT_GRACE_SECONDS
            return exited

        try:
            services._wait_for_daemon(
                timeout=DAEMON_START_IDLE_BUDGET_SECONDS,
                startup_progress=lambda: _daemon_startup_phase_summary(
                    state_dir, spawned_at=spawned_at
                ),
                job_exited=job_exited,
            )
        except services.CliError as error:
            if is_start_verdict(error):
                return False
            raise
        return True

    return wait


def unattributed_unit_fields(status: Any | None) -> JsonObject:
    """Additive ``daemon stop`` fields for a LOADED, unattributable unit.

    Empty (the keys are absent, like ``survivingPid``) when there is no such
    unit or it is not loaded, so nothing else about the answer changes. Names
    and values are those of ``daemon service status --json``; the unit path is
    deliberately left out.
    """

    if status is None or status.loaded is not True:
        return {}
    return {
        "supervisorMayRestart": True,
        "unattributedSupervisor": {
            "platform": status.platform,
            "unit": status.unit,
            "attributed": False,
            "ownershipReason": getattr(status, "ownership_reason", None),
        },
    }
