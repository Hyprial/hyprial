"""The ``hyprial daemon upgrade`` family (the ``hyprial config set`` group lives
in ``commands/config/settings.py``)."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import CliResult

from hyprial.shell.impl.cli.commands.common.services import get_services

from datetime import datetime
from hyprial.kernel import ipc_errors
import os
import sys
import typer

from hyprial.shell.impl.cli.commands.common.pending_restart import _clear_pending_restart, _read_pending_restart, _restart_daemon_onto_install
from hyprial.shell.impl.cli.commands.daemon.restart import _daemon_restart_identity, _record_autoupdate_restart_confirmation, _record_autoupdate_restart_not_required
from hyprial.shell.impl.cli.commands.common.support import JsonObject, _probe_reports_running, _utc_now
from hyprial.shell.impl.cli.commands.upgrade.commands import upgrade_app
autoupdate_app = typer.Typer(
    help="Inspect daemon-owned automatic upgrades."
)


@upgrade_app.command("status")
@autoupdate_app.command("status")
def autoupdate_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show whether automatic upgrade is on, when it runs, and what it last did."""
    services = get_services()

    def collect() -> JsonObject:
        services = get_services()
        from hyprial.daemon import updates
        from hyprial.daemon import (
            SCHEDULE,
            read_last_run,
        )

        scheduler: JsonObject
        try:
            scheduler_result = services._daemon_request(
                "autoupdate.status", timeout=1.0, restore_wait=0.0
            )
        # PR #332 F4②: the best-effort transport-transient set, spelled as
        # the registered classes.  DAEMON_RESTORING stays out (its budget is
        # the caller's restore_wait=0.0 fail-fast choice).
        except (
            ipc_errors.DaemonUnavailableError,
            ipc_errors.DaemonDisconnectedError,
            ipc_errors.IpcTimeoutError,
        ):
            scheduler = {
                "running": False,
                "active": False,
                "pending": False,
                "nextRunAt": None,
                "lastRun": read_last_run(services._state_dir()),
                # Installed but not yet running; `hyprial daemon upgrade apply` applies it.
                "pendingRestart": _read_pending_restart(),
            }
        else:
            if not isinstance(scheduler_result, dict):
                raise services.CliError(
                    "INVALID_RESPONSE", "daemon autoupdate status must be an object"
                )
            scheduler = scheduler_result

        from hyprial.daemon import upgrade_step_status

        auto_upgrade_enabled = updates.auto_upgrade_enabled(services._hyprial_home())
        pending = _read_pending_restart()
        return {
            "ok": True,
            "trigger": "daemon",
            "autoUpgradeEnabled": auto_upgrade_enabled,
            "platform": sys.platform,
            # Compatibility fields retained for the published status contract;
            # there is no longer any installable external timer.
            "unit": None,
            "installed": False,
            "loaded": False,
            "schedule": [
                {"hour": hour, "minute": minute} for hour, minute in SCHEDULE
            ],
            "lastRun": read_last_run(services._state_dir()),
            "pendingRestart": pending,
            "pendingRestartState": _pending_restart_state(pending),
            "scheduler": scheduler,
            "upgradeSteps": upgrade_step_status(services._state_dir()),
        }

    def operation() -> CliResult:
        return CliResult(collect(), render=_render_autoupdate_status)

    services._execute(operation, json_output=json_output)


def _pending_restart_state(pending: JsonObject | None) -> str | None:
    """Is the recorded pending restart still owed?  The same test ``hyprial
    autoupdate restart`` applies, so status and restart cannot disagree:

    * ``waiting``: the daemon recorded before the install is still running;
    * ``applied``: a different daemon runs now, so it already has the code;
    * ``unverified``: no daemon answered in time (restart re-checks).

    Only probes when a record exists, with a short budget: status must stay
    cheap, and ``ping`` answers mid-restore without an actor snapshot.
    """
    services = get_services()

    if pending is None:
        return None
    recorded = pending.get("before")
    recorded_pid = recorded.get("pid") if isinstance(recorded, dict) else None
    try:
        probe = services._daemon_probe(timeout=1.0)
    except (
        ipc_errors.DaemonUnavailableError,
        ipc_errors.DaemonDisconnectedError,
        ipc_errors.IpcTimeoutError,
        services.CliError,
    ):
        return "unverified"
    if not _probe_reports_running(probe):
        return "unverified"
    daemon = probe.get("daemon") if isinstance(probe.get("daemon"), dict) else probe
    pid = daemon.get("pid")
    if not isinstance(pid, int) or not isinstance(recorded_pid, int):
        return "unverified"
    return "waiting" if pid == recorded_pid else "applied"


def _local_minute(value: object) -> str:
    """ISO timestamp → ``YYYY-MM-DD HH:MM`` local; unparseable ones verbatim."""

    if not isinstance(value, str):
        return "?"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%Y-%m-%d %H:%M")


def _render_autoupdate_status(status: JsonObject) -> str:
    """``hyprial daemon upgrade status`` for a person: what is on, when it runs, what
    it last did, and the one command that acts on it.

    #28: the old output was the raw dict, whose first lines read
    ``installed: False, loaded: False`` -- the *legacy* launchd/systemd unit --
    while the daemon's scheduler was running the upgrades.  Scheduling now
    lives in the daemon, so the unit is shown last and labelled legacy.
    ``--json`` is unchanged apart from ``pendingRestart``.
    """

    lines: list[str] = []
    enabled = status.get("autoUpgradeEnabled") is True
    if enabled:
        lines.append("hyprial daemon upgrade   automatic upgrade: on")
    else:
        lines.append("hyprial daemon upgrade   automatic upgrade: off (scheduled runs are skipped)")
        lines.append("               turn on with: hyprial config set autoUpgrade true")
    times = " and ".join(
        f"{slot.get('hour', 0):02d}:{slot.get('minute', 0):02d}"
        for slot in status.get("schedule") or []
        if isinstance(slot, dict)
    )
    lines.append(f"  schedule     {times or '?'} local time, run by the daemon")

    scheduler = status.get("scheduler")
    scheduler = scheduler if isinstance(scheduler, dict) else {}
    if scheduler.get("active"):
        lines.append("  scheduler    an upgrade is running now")
    elif scheduler.get("running"):
        next_run = scheduler.get("nextRunAt")
        suffix = f", next run {_local_minute(next_run)}" if next_run else ""
        lines.append(f"  scheduler    running{suffix}")
    else:
        lines.append("  scheduler    not reachable; scheduled runs happen only while the daemon runs")

    last = status.get("lastRun")
    if not isinstance(last, dict):
        lines.append("  last run     never")
    else:
        when = _local_minute(last.get("at"))
        if last.get("ok") is not True:
            code = f"{last['code']}: " if last.get("code") else ""
            lines.append(f"  last run     {when}  failed: {code}{last.get('error', 'no detail')}")
        elif last.get("skipped"):
            lines.append(f"  last run     {when}  skipped: {last.get('skipReason', 'no reason recorded')}")
        else:
            commit = str(last.get("resolvedCommit") or "")[:8]
            target = " ".join(part for part in (last.get("resolvedTag"), commit) if part)
            verb = "installed" if last.get("upgraded") else "already current at"
            lines.append(f"  last run     {when}  ok, {verb} {target or '?'}")

    # The pending-restart file, not lastRun.restart, is the authority: a
    # restart by any other route leaves lastRun saying "awaiting".
    pending = status.get("pendingRestart")
    if isinstance(pending, dict):
        installed = " ".join(
            part for part in (pending.get("version"), str(pending.get("commit") or "")[:8]) if part
        ) or "a new version"
        state = status.get("pendingRestartState")
        if state == "applied":
            lines.append(f"  restart      done: the running daemon already has {installed}")
        elif state == "waiting":
            lines.append(f"  restart      waiting: {installed} installed; the daemon runs the old code")
            lines.append("               apply it with: hyprial daemon upgrade apply")
        else:
            lines.append(f"  restart      {installed} was installed; could not check the daemon")
            lines.append("               check and apply with: hyprial daemon upgrade apply")

    return "\n".join(lines)


@upgrade_app.command("apply")
@autoupdate_app.command("restart")
def autoupdate_restart(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Restart the daemon onto the version the timer installed (your confirmation).

    The timer installs new versions but never restarts on its own; this is the
    confirmation.  Run it yourself or have any agent run it.  With nothing
    pending it does nothing.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from types import SimpleNamespace

        from hyprial.daemon import updates
        from hyprial.shell.impl.cli.commands.upgrade.commands import _run_upgrade_steps

        def with_steps(payload: JsonObject) -> JsonObject:
            payload["upgradeSteps"] = _run_upgrade_steps(services)
            return payload

        pending = _read_pending_restart()
        if pending is None:
            return with_steps(
                {"ok": True, "restarted": False, "reason": "no pending restart"}
            )
        recorded = pending.get("before")
        before = services._running_daemon_before_upgrade(
            str(recorded.get("version") if isinstance(recorded, dict) else "unknown")
        )
        if before is None:
            # No daemon: the next start runs the installed code anyway.
            reason = "daemon is not running; the next start uses the installed version"
            _record_autoupdate_restart_not_required(
                via="hyprial daemon upgrade apply",
                reason=reason,
            )
            _clear_pending_restart()
            return with_steps({
                "ok": True,
                "restarted": False,
                "reason": reason,
                "pendingRestart": pending,
            })
        if isinstance(recorded, dict) and recorded.get("pid") != before.get("pid"):
            # Something already restarted it after the install; that daemon
            # runs the installed code.
            current = _daemon_restart_identity(services._daemon_probe(timeout=2.0))
            installed = updates.read_installation()
            if current.get("version") is None:
                current["version"] = str(
                    installed.version or pending.get("version") or "unknown"
                )
            _record_autoupdate_restart_confirmation(
                via="hyprial daemon upgrade apply",
                after=current,
            )
            _clear_pending_restart()
            return with_steps({
                "ok": True,
                "restarted": False,
                "reason": "daemon was already restarted after the install",
                "before": before,
                "pendingRestart": pending,
            })
        installed = updates.read_installation()
        installed_version = str(installed.version or pending.get("version") or "unknown")
        resolved = pending.get("resolved") if isinstance(pending.get("resolved"), dict) else {}
        result = _restart_daemon_onto_install(
            {"ok": True, "confirmedRestart": True, "pendingRestart": pending},
            before=before,
            installed_version=installed_version,
            resolution=SimpleNamespace(
                tag=str(pending.get("tag") or ""), commit=str(pending.get("commit") or "")
            ),
            resolved=resolved,
            confirmation_via="hyprial daemon upgrade apply",
        )
        _clear_pending_restart()
        return with_steps(result)

    services._execute(operation, json_output=json_output)


@upgrade_app.command("run", hidden=True)
@autoupdate_app.command("run", hidden=True)
def autoupdate_run(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Timer entry point: upgrade to the latest tag and record the last run."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import updates
        from hyprial.daemon import AUTOUPDATE_CHILD_ENV, write_last_run

        # Only the daemon-owned scheduler may create the in-process child.
        # A manual invocation (or a surviving old timer) delegates to the live
        # daemon and becomes a successful no-op when that owner is unavailable.
        if os.environ.get(AUTOUPDATE_CHILD_ENV) != "1":
            try:
                scheduled = services._daemon_request(
                    "autoupdate.trigger", timeout=2.0, restore_wait=0.0
                )
            except (
                ipc_errors.DaemonUnavailableError,
                ipc_errors.DaemonDisconnectedError,
                ipc_errors.IpcTimeoutError,
            ):
                return {
                    "ok": True,
                    "skipped": True,
                    "skipReason": "daemon unavailable",
                }
            else:
                if not isinstance(scheduled, dict):
                    raise services.CliError(
                        "INVALID_RESPONSE",
                        "daemon autoupdate trigger result must be an object",
                    )
                return scheduled

        services.require_initialized_hyprial_home()

        # Guard 1 (spec autoupdate-isolated-home r1, Allen 2026-09-15):
        # automatic upgrades are OFF unless settings.json explicitly opts in.
        # The manual ``hyprial daemon upgrade`` is an operator's explicit act and is
        # not gated by this switch.
        if not updates.auto_upgrade_enabled(services._hyprial_home()):
            skip_record: JsonObject = {
                "ok": True,
                "at": _utc_now(),
                "skipped": True,
                "skipReason": updates.AUTOUPGRADE_DISABLED_REASON,
            }
            write_last_run(services._state_dir(), skip_record)
            return skip_record

        # The twice-daily timer is the already-happening heartbeat the
        # lark-cli user-credential watchdog hangs on (#142: a mechanism
        # nothing triggers is dead code).  Best-effort: runs before the
        # upgrade (the daemon is up at timer fire time; the upgrade may
        # restart it) and never fails the run.
        lark_auth = services._lark_auth_timer_check()

        def attach(record: JsonObject) -> JsonObject:
            if lark_auth is not None:
                record["larkAuth"] = lark_auth
            return record

        try:
            result = services._perform_upgrade_and_report(
                force=False,
                source="official",
                restart=False,
                awaiting_confirmation=True,
            )
        except (services.CliError, ipc_errors.TransientDaemonError) as error:
            failure_record: JsonObject = attach({
                "ok": False,
                "at": _utc_now(),
                "code": error.code,
                "error": str(error),
            })
            if error.data is not None:
                failure_record["data"] = error.data
            write_last_run(
                services._state_dir(),
                failure_record,
            )
            raise
        except Exception as error:  # noqa: BLE001 - the timer must never fail silently
            write_last_run(
                services._state_dir(),
                attach(
                    {
                        "ok": False,
                        "at": _utc_now(),
                        "code": "UNEXPECTED",
                        "error": str(error),
                    }
                ),
            )
            raise
        record: JsonObject = attach(
            {
                "ok": True,
                "at": _utc_now(),
                "resolvedTag": result.get("resolvedTag"),
                "resolvedCommit": result.get("resolvedCommit"),
                "upgraded": result.get("upgraded", False),
                "restartRequired": result.get("restartRequired", False),
            }
        )
        if result.get("declinedDowngrade") is True:
            record["declinedDowngrade"] = True
            record["reason"] = result.get("reason")
        if "warning" in result:
            record["warning"] = result["warning"]
        if "restart" in result:
            record["restart"] = result["restart"]
        if "notification" in result:
            record["notification"] = result["notification"]
        from hyprial.shell.impl.cli.commands.upgrade.commands import _run_upgrade_steps

        record["upgradeSteps"] = _run_upgrade_steps(services)
        write_last_run(services._state_dir(), record)
        return record

    services._execute(operation, json_output=json_output, allow_missing_home=True)
