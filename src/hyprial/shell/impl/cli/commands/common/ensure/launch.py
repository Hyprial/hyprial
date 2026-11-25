"""Make sure something listens before a command's single request (Z-4).

Decisions are recorded in ``scope.py`` (daemon-first-z4-cli-ensure-20261117).
The lazy imports live here, away from the scope machinery the upgrade tail runs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hyprial.shell.impl.cli.commands.common.ensure.scope import (
    ACCOUNT_REQUIRED,
    BUSY,
    ON,
    current_scope,
    enter_scope,
    json_output_requested,
    leave_scope,
)

_UNSCOPED_METHODS = frozenset({"ping", "shutdown"})
NEXT_STEPS = ["hyprial login"]


def ensure_listening(services: Any, method: str, socket_absent: Callable[[Any], bool]) -> None:
    """Start the daemon once when nothing listens; never sends a request frame."""

    if current_scope() != ON or method in _UNSCOPED_METHODS:
        return
    from hyprial.shell.impl.cli.commands.common.daemon_start import _daemon_autostart_disabled

    if _daemon_autostart_disabled():
        return
    try:
        binding = services.runtime.worker_session_binding()
    except Exception:  # noqa: BLE001
        return
    if binding is not None or not socket_absent(services._socket_path()):
        return

    from hyprial.shell.impl.cli.commands.common.support import DAEMON_START_IDLE_BUDGET_SECONDS
    from hyprial.shell.impl.cli.output import progress

    token = enter_scope(BUSY)
    try:
        progress("daemon-ensure", "Starting the daemon for this home...", json_output=json_output_requested())
        services._launch_daemon_process(
            ready_timeout=DAEMON_START_IDLE_BUDGET_SECONDS,
            implicit=True,
            autoinstall=False,
        )
    finally:
        leave_scope(token)


def with_next_step(services: Any, error: Exception) -> Exception | None:
    """The daemon's own ``ACCOUNT_REQUIRED`` plus the next step, or None."""

    if current_scope() != ON or getattr(error, "code", None) != ACCOUNT_REQUIRED:
        return None
    data = getattr(error, "data", None)
    return services.CliError(
        ACCOUNT_REQUIRED,
        str(error),
        {**(data if isinstance(data, dict) else {}), "nextSteps": list(NEXT_STEPS)},
    )
