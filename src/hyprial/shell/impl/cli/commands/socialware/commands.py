"""``hyprial socialware install`` placeholder and upgrade helpers."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.kernel import ipc_errors
import sys
import typer

from hyprial.shell.impl.cli.commands.common.support import JsonObject, _probe_reports_running


socialware_app = typer.Typer(
    help="Install and manage socialware applications (coming soon)."
)


@socialware_app.command(
    "install",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
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
        raise services.CliError(
            "COMING_SOON", "hyprial socialware install is coming soon"
        )

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
    epoch = daemon.get("epoch")
    reported_version = daemon.get("version")
    if not isinstance(pid, int) or pid <= 0:
        raise services.CliError("INVALID_RESPONSE", "running daemon did not report a valid pid")
    if not isinstance(epoch, str) or not epoch:
        raise services.CliError(
            "INVALID_RESPONSE", "running daemon did not report a valid epoch"
        )
    if not isinstance(reported_version, str) or not reported_version:
        raise services.CliError(
            "INVALID_RESPONSE", "running daemon did not report its version"
        )
    return {"pid": pid, "epoch": epoch, "version": reported_version}


def _prime_post_install_restart_imports() -> frozenset[str]:
    """Load restart-only modules before ``uv tool install`` replaces us.

    A uv tool upgrade atomically removes the distribution backing this still
    running CLI process.  Any first import after that boundary resolves
    against a path that no longer exists, even though the newly installed
    command is complete.  The restart path intentionally remains in this
    process so it can preserve the pre-install daemon snapshot and return one
    atomic upgrade result; load its lazy dependencies before crossing the
    destructive install boundary.
    """

    # Resolve every lazy dependency used after installation while this exact
    # distribution still backs the process.  ``uv tool install --force`` may
    # replace package files in place; importing even one new module afterwards
    # can combine an old package object in ``sys.modules`` with new module code.
    from hyprial.daemon import (
        AUTOUPDATE_CHILD_ENV,
        AUTOUPDATE_TRIGGER_ENV,
        DesiredStateStore,
        ForwardingConfigurationError,
        OwnerMigrationHostedConflict,
        RestartProcessObservation,
        RestartProcessState,
        ServiceConfig,
        ServiceManager,
        ServiceOwnershipError,
        ensure_dispatch_policy,
        harness_desired_generation,
        latest_start_failure,
        notify_restore_followup,
        notify_upgrade_outcome,
        owner_process_status,
        prime_upgrade_alert_imports,
        process_cpu_seconds,
        read_last_run,
        read_process_identity,
        resolve_profile,
        restart_failure_detail,
        restore_followup_budget_seconds,
        run_mcp_entry,
        run_self_check,
        run_upgrade_steps,
        start_failure_line,
        tcp_probe,
        write_last_run,
    )
    from hyprial.identity import (
        IDENTITY_TRANSACTION_FD_ENV,
        IdentityTransactionBusy,
        IdentityTransactionLock,
        UserProfileError,
        UserProfileStore,
    )
    from hyprial.kernel import DaemonLaunchResult, HarnessLaunchSpec, parse_channel_uri
    from hyprial.shell.impl.cli.commands.daemon.restart import (
        _record_autoupdate_restart_confirmation,
    )
    from hyprial.shell.impl.cli.commands.daemon.service import (
        restart_supervised_daemon,
    )
    from hyprial.shell.impl.gui.apps import upgrade as unpack_gui

    _ = (
        AUTOUPDATE_CHILD_ENV,
        AUTOUPDATE_TRIGGER_ENV,
        DesiredStateStore,
        ForwardingConfigurationError,
        OwnerMigrationHostedConflict,
        RestartProcessObservation,
        RestartProcessState,
        ServiceConfig,
        ServiceManager,
        ServiceOwnershipError,
        ensure_dispatch_policy,
        harness_desired_generation,
        latest_start_failure,
        notify_restore_followup,
        notify_upgrade_outcome,
        owner_process_status,
        prime_upgrade_alert_imports,
        process_cpu_seconds,
        read_last_run,
        read_process_identity,
        resolve_profile,
        restart_failure_detail,
        restore_followup_budget_seconds,
        run_mcp_entry,
        run_self_check,
        run_upgrade_steps,
        start_failure_line,
        tcp_probe,
        write_last_run,
        IDENTITY_TRANSACTION_FD_ENV,
        IdentityTransactionBusy,
        IdentityTransactionLock,
        UserProfileError,
        UserProfileStore,
        DaemonLaunchResult,
        HarnessLaunchSpec,
        parse_channel_uri,
        _record_autoupdate_restart_confirmation,
        restart_supervised_daemon,
        unpack_gui,
    )
    prime_upgrade_alert_imports()
    return frozenset(
        name for name in sys.modules if name == "hyprial" or name.startswith("hyprial.")
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
            f"`hyprial daemon upgrade`. {resolved_suffix}",
            {**resolved, "installedCommit": installed.commit},
        )
