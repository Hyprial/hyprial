"""``hyprial autoupdate`` family and ``hyprial config set``."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import CliResult

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from pathlib import Path
from datetime import datetime
from hyprial.kernel import ipc_errors
import os
import shutil
import sys
import typer

from hyprial.shell.impl.cli.commands.common.pending_restart import _clear_pending_restart, _read_pending_restart, _restart_daemon_onto_install
from hyprial.shell.impl.cli.commands.daemon.restart import _daemon_restart_identity, _record_autoupdate_restart_confirmation, _record_autoupdate_restart_not_required
from hyprial.shell.impl.cli.commands.common.support import JsonObject, _probe_reports_running, _utc_now
autoupdate_app = typer.Typer(
    help="Inspect daemon-owned autoupdate and manage its legacy timer unit."
)




def _timer_config(*, require_executables: bool) -> Any:
    """Build the timer unit config, resolving the executable paths at install
    time so the rendered units stay valid even if the PATH changes later."""
    services = get_services()

    from hyprial.daemon import TimerConfig

    executable = shutil.which("hyprial")
    uv = shutil.which("uv")
    if require_executables:
        if executable is None:
            raise services.CliError(
                "INVALID_CONFIGURATION",
                "hyprial executable not on PATH; install with 'uv tool install' first",
            )
        if uv is None:
            raise services.CliError(
                "INVALID_CONFIGURATION",
                "uv not on PATH; 'hyprial upgrade' cannot reinstall without it",
            )
    path_entries: list[str] = []
    for candidate in (uv, executable):
        if candidate is None:
            continue
        parent = str(Path(candidate).resolve().parent)
        if parent not in path_entries:
            path_entries.append(parent)
    # User-level installs (uv tools, pipx, the codex standalone shim) live in
    # ~/.local/bin on both macOS and Linux; units rendered without it cannot
    # find those binaries (2026-08-23 post-upgrade outage).
    user_bin = str(Path.home() / ".local" / "bin")
    if user_bin not in path_entries:
        path_entries.append(user_bin)
    path_entries.append(
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    )
    return TimerConfig(
        executable=(
            Path(executable).resolve()
            if executable is not None
            else Path(sys.argv[0]).resolve()
        ),
        home=Path.home(),
        hyprial_home=services._hyprial_home(),
        state_dir=services._state_dir(),
        path_env=":".join(path_entries),
    )


def _autoupdate_result(status: Any) -> JsonObject:
    from hyprial.daemon import SCHEDULE

    result: JsonObject = {
        "ok": True,
        "platform": status.platform,
        "unit": status.unit,
        "installed": status.installed,
        "loaded": status.loaded,
        "schedule": [{"hour": hour, "minute": minute} for hour, minute in SCHEDULE],
        "lastRun": status.last_run,
    }
    if status.boot_persistent is not None:
        result["bootPersistent"] = status.boot_persistent
    if status.enabled is not None:
        result["enabled"] = status.enabled
    if status.changed is not None:
        result["changed"] = status.changed
    return result


@autoupdate_app.command("install")
def autoupdate_install(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Install the legacy cutover timer (the daemon now owns scheduling)."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import AutoUpdateManager, detect_platform

        platform = detect_platform()
        if platform is None:
            raise services.CliError(
                "PLATFORM_UNSUPPORTED",
                f"no update timer for platform {sys.platform!r}; "
                "supported: macOS (launchd), Linux (systemd)",
            )
        manager = AutoUpdateManager(
            services._timer_config(require_executables=True), platform=platform
        )
        try:
            return _autoupdate_result(manager.install())
        except (RuntimeError, OSError) as error:
            raise services.CliError("AUTOUPDATE_INSTALL_FAILED", str(error)) from error

    services._execute(operation, json_output=json_output)


@autoupdate_app.command("uninstall")
def autoupdate_uninstall(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove the legacy timer after daemon-owned upgrade proof."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import AutoUpdateManager, detect_platform

        platform = detect_platform()
        if platform is None:
            raise services.CliError(
                "PLATFORM_UNSUPPORTED",
                f"no update timer for platform {sys.platform!r}",
            )
        manager = AutoUpdateManager(
            services._timer_config(require_executables=False), platform=platform
        )
        try:
            return _autoupdate_result(manager.uninstall())
        except (RuntimeError, OSError) as error:
            raise services.CliError("AUTOUPDATE_UNINSTALL_FAILED", str(error)) from error

    services._execute(operation, json_output=json_output)


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
            AutoUpdateManager,
            detect_platform,
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
                # Installed but not yet running; `hyprial autoupdate restart` applies it.
                "pendingRestart": _read_pending_restart(),
            }
        else:
            if not isinstance(scheduler_result, dict):
                raise services.CliError(
                    "INVALID_RESPONSE", "daemon autoupdate status must be an object"
                )
            scheduler = scheduler_result

        platform = detect_platform()
        auto_upgrade_enabled = updates.auto_upgrade_enabled(services._hyprial_home())
        pending = _read_pending_restart()
        if platform is None:
            return {
                "ok": True,
                "trigger": "daemon",
                "autoUpgradeEnabled": auto_upgrade_enabled,
                "platform": sys.platform,
                "unit": None,
                "installed": False,
                "loaded": False,
                "schedule": [
                    {"hour": hour, "minute": minute} for hour, minute in SCHEDULE
                ],
                "lastRun": read_last_run(services._state_dir()),
                # Installed but not yet running; `hyprial autoupdate restart` applies it.
                "pendingRestart": pending,
                "pendingRestartState": _pending_restart_state(pending),
                "scheduler": scheduler,
            }
        manager = AutoUpdateManager(
            services._timer_config(require_executables=False), platform=platform
        )
        result = _autoupdate_result(manager.status())
        result["trigger"] = "daemon"
        result["autoUpgradeEnabled"] = auto_upgrade_enabled
        result["scheduler"] = scheduler
        # #28: the platform path used to omit this, so on macOS/Linux with the
        # daemon up the field hyprial-ops documents was simply absent.
        result["pendingRestart"] = pending
        result["pendingRestartState"] = _pending_restart_state(pending)
        result["legacyUnit"] = {
            "unit": result["unit"],
            "installed": result["installed"],
            "loaded": result["loaded"],
        }
        return result

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
    """``hyprial autoupdate status`` for a person: what is on, when it runs, what
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
        lines.append("hyprial autoupdate   automatic upgrade: on")
    else:
        lines.append("hyprial autoupdate   automatic upgrade: off (scheduled runs are skipped)")
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
            lines.append("               apply it with: hyprial autoupdate restart")
        else:
            lines.append(f"  restart      {installed} was installed; could not check the daemon")
            lines.append("               check and apply with: hyprial autoupdate restart")

    unit = status.get("unit")
    if unit:
        if status.get("installed"):
            state = "installed" + (" and loaded" if status.get("loaded") else ", not loaded")
            lines.append(f"  legacy unit  {unit}: {state}")
            lines.append("               (from before the daemon scheduled upgrades)")
        else:
            lines.append(f"  legacy unit  {unit}: not installed (not needed)")
    return "\n".join(lines)


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

        pending = _read_pending_restart()
        if pending is None:
            return {"ok": True, "restarted": False, "reason": "no pending restart"}
        recorded = pending.get("before")
        before = services._running_daemon_before_upgrade(
            str(recorded.get("version") if isinstance(recorded, dict) else "unknown")
        )
        if before is None:
            # No daemon: the next start runs the installed code anyway.
            reason = "daemon is not running; the next start uses the installed version"
            _record_autoupdate_restart_not_required(
                via="hyprial autoupdate restart",
                reason=reason,
            )
            _clear_pending_restart()
            return {
                "ok": True,
                "restarted": False,
                "reason": reason,
                "pendingRestart": pending,
            }
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
                via="hyprial autoupdate restart",
                after=current,
            )
            _clear_pending_restart()
            return {
                "ok": True,
                "restarted": False,
                "reason": "daemon was already restarted after the install",
                "before": before,
                "pendingRestart": pending,
            }
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
            confirmation_via="hyprial autoupdate restart",
        )
        _clear_pending_restart()
        return result

    services._execute(operation, json_output=json_output)


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

        services.require_initialized_hyprial_home()

        # Guard 1 (spec autoupdate-isolated-home r1, Allen 2026-09-15):
        # automatic upgrades are OFF unless settings.json explicitly opts in
        # (``hyprial config set autoUpgrade true``).  The check sits at THIS
        # entry point, not only in the daemon scheduler, because the legacy
        # launchd/systemd timer runs ``autoupdate run`` directly and the
        # daemon-trigger delegation below silently falls through to a local
        # upgrade when the daemon is unreachable.  The manual
        # ``hyprial upgrade`` is an operator's explicit act and is NOT gated
        # by this switch.
        if not updates.auto_upgrade_enabled(services._hyprial_home()):
            skip_record: JsonObject = {
                "ok": True,
                "at": _utc_now(),
                "skipped": True,
                "skipReason": updates.AUTOUPGRADE_DISABLED_REASON,
            }
            write_last_run(services._state_dir(), skip_record)
            return skip_record

        # During the staged migration the old launchd/systemd unit remains in
        # place until a daemon-owned update has succeeded.  Its invocation is
        # converted into a scheduler signal, so the actual child always
        # inherits the daemon environment and duplicate calendar fires dedup.
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
                pass
            else:
                if not isinstance(scheduled, dict):
                    raise services.CliError(
                        "INVALID_RESPONSE",
                        "daemon autoupdate trigger result must be an object",
                    )
                return scheduled

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
        write_last_run(services._state_dir(), record)
        return record

    services._execute(operation, json_output=json_output, allow_missing_home=True)
