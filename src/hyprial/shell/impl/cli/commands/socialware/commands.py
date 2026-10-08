"""``hyprial install`` (retired stub) and self-upgrade install helpers."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.kernel import ipc_errors
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import JsonObject, _probe_reports_running


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def install(
    names: list[str] = typer.Argument(None, help="Accepted and ignored."),
    yes: bool = typer.Option(False, "--yes", help="Accepted and ignored."),
    check: bool = typer.Option(False, "--check", help="Accepted and ignored."),
    force: bool = typer.Option(False, "--force", help="Accepted and ignored."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Application installation is retired; this entry will return in a future release.

    The application install system (catalog, artifact download, manifests,
    receipts and command mounting) was removed on 2026-10-03.  The command
    name stays reserved so scripts fail loudly with COMING_SOON instead of
    an unknown-command error; every argument and option is accepted and
    ignored, and the command never reports success.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        raise services.CliError("COMING_SOON", "hyprial install is coming soon")

    services._execute(operation, json_output=json_output, allow_missing_home=True)


def _running_daemon_before_upgrade(version: str) -> JsonObject | None:
    services = get_services()
    try:
        # The pre-restart snapshot needs ONE number, the pid, and must not
        # hang on the actor projection: on 2026-09-05 03:17 this asked ``ps``
        # (77 actors x N desired-state loads, card 259) and the 15 s budget
        # expired, so autoupdate self-locked on exactly the build that fixed
        # the storm (card b9872e94).  ``_daemon_probe`` asks ping -- light,
        # answered mid-restore, no actor snapshot -- and falls back to ``ps``
        # only for a daemon older than the ping contract.  The normal IPC
        # budget stays: a loaded daemon still gets its 15 s to answer ping.
        status = services._daemon_probe(timeout=15.0)
    except ipc_errors.DaemonUnavailableError:
        return None
    if not _probe_reports_running(status):
        return None
    daemon = status.get("daemon") if isinstance(status.get("daemon"), dict) else status
    pid = daemon.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        raise services.CliError("INVALID_RESPONSE", "running daemon did not report a valid pid")
    return {"pid": pid, "version": version}


def _prime_post_install_restart_imports() -> None:
    """Load restart-only modules before ``uv tool install`` replaces us.

    A uv tool upgrade atomically removes the distribution backing this still
    running CLI process.  Any first import after that boundary resolves
    against a path that no longer exists, even though the newly installed
    command is complete.  The restart path intentionally remains in this
    process so it can preserve the pre-install daemon snapshot and return one
    atomic upgrade result; load its lazy dependencies before crossing the
    destructive install boundary.
    """

    # Resolve the real public APIs while this installed distribution still
    # exists. Explicit imports retain lazy loading without runtime path strings.
    from hyprial.daemon import (
        AutoUpdateManager,
        notify_upgrade_outcome,
        owner_process_status,
        restore_followup_budget_seconds,
        run_self_check,
    )
    from hyprial.kernel import HarnessLaunchSpec, parse_channel_uri

    _ = (
        AutoUpdateManager, owner_process_status, restore_followup_budget_seconds,
        HarnessLaunchSpec, parse_channel_uri, notify_upgrade_outcome, run_self_check,
    )


def _require_installed_commit(
    installed: Any, resolution: Any, resolved: JsonObject, resolved_suffix: str
) -> None:
    """Refuse to report an upgrade whose installed commit was never resolved.

    The restart decision follows the CODE, not the ref's name: an explicit
    movable tag (``--tag internal``) keeps the same requested_revision while
    uv installs a new commit, and comparing names alone left the daemon on
    the old code (jjkysy-dev, 2026-09-26: b6cd834 -> e9dec894, "resolved tag
    unchanged").  Symmetrically, if uv fetched something other than what was
    resolved (the tag moved again, a stale cache), never report upgraded=true
    for it; restarting onto it would also defeat the downgrade guard, which
    judged the resolved commit.
    """
    services = get_services()

    if (
        installed.commit is not None
        and resolution.commit is not None
        and installed.commit != resolution.commit
    ):
        raise services.CliError(
            "UPGRADE_COMMIT_MISMATCH",
            f"uv installed commit {installed.commit} but {resolution.commit} "
            f"was resolved; not restarting onto unresolved code. Re-run "
            f"`hyprial upgrade`. {resolved_suffix}",
            {**resolved, "installedCommit": installed.commit},
        )
