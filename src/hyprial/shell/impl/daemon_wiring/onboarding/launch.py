"""Ownerless-home lifecycle wiring for ``hyprial daemon run``.

Decision: z7-a1-delete-http-bootstrap-host, 2026-11-19.  The daemon is the only
process that hosts login.  The terminal login loop that ``daemon run --bootstrap``
once ran (admit a ``BootstrapRuntime``, print a device code, poll the identity
worker, accept initialize and promote in place) is deleted: a home with no owner
is served by ``launch_ownerless_daemon``, whose lifecycle loop answers the
daemon's own bootstrap wire, and terminal login is ``hyprial login``.  The
``--bootstrap`` flag of ``daemon run`` is still accepted and ignored; removing it
is a CLI-surface change that belongs to its own PR.

This module is lifecycle assembly only.  No credential file is read here and no
credential module is imported.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hyprial.daemon import (
    BootstrapLaunchError,
    STOP_LOGOUT,
    launch_daemon_lifecycle,
    leave_interpreter_after_loop as _leave_interpreter_after_loop,
)
from hyprial.identity import node_owner_or_none

__all__ = [
    "BootstrapLaunchError",
    "build_daemon_application",
    "home_owner_or_none",
    "OwnerlessLaunchError",
    "launch_ownerless_daemon",
    "leave_ownerless_loop",
    "stopped_for_logout",
]


def build_daemon_application(state_dir: Path, socket_path: Path) -> Any:
    """Construct the one real daemon application for this process.

    Kept as a named function rather than an inline lambda because both the
    production entry and the acceptance test must prove they use the same
    constructor path.  Import stays lazy so ``import hyprial.cli`` does not
    eagerly load application modules.
    """

    from hyprial.daemon import DaemonApplication

    from hyprial.shell.impl.daemon_wiring import daemon_dependencies, usage_factory

    return DaemonApplication.from_environment(
        state_dir=state_dir,
        socket_path=socket_path,
        usage_factory=usage_factory,
        **daemon_dependencies(),
    )


def home_owner_or_none(environ: Mapping[str, str], home: Path) -> str | None:
    """The owner the daemon itself would resolve: ``HYPRIAL_OWNER``, then settings."""

    return node_owner_or_none(environ, hyprial_home=home)


class OwnerlessLaunchError(BootstrapLaunchError):
    """The lifecycle loop's refusal, as the CLI composition may name and catch it."""


def launch_ownerless_daemon(**options: Any) -> None:
    """Run the daemon lifecycle loop for a home with no owner (same PID throughout)."""

    try:
        launch_daemon_lifecycle(**options)
    except BootstrapLaunchError as error:
        raise OwnerlessLaunchError(error.code, str(error)) from error


def stopped_for_logout(runtime: Any) -> bool:
    """Whether the serving application that just returned stopped for a pure logout."""

    return bool(runtime.stop_reason == STOP_LOGOUT)


def leave_ownerless_loop(*, failed: bool = False) -> None:
    """After the loop's result is written: flush and leave like a final stop does."""

    _leave_interpreter_after_loop(failed=failed)
