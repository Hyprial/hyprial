"""User-service ownership for ``hyprial daemon service`` and doctor."""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import Any

import typer
from hyprial.shell.impl.cli.commands.common.groups import group

from hyprial.kernel import DaemonLaunchResult, ipc_errors
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.commands.daemon.commands import daemon_app
from hyprial.shell.impl.cli.output import CliResult, confirm, render_generic


daemon_service_app = group(
    help="Install, remove, and inspect the per-user daemon supervisor service.",
    no_args_is_help=True,
)


class ServicePlatformUnsupported(RuntimeError):
    def __init__(self, platform: str) -> None:
        super().__init__(f"daemon service supervision is unsupported on {platform}")
        self.platform = platform


def _service_platform() -> str:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    raise ServicePlatformUnsupported(sys.platform)


def _service_is_interactive() -> bool:
    return sys.stdin.isatty()


def _serving_daemon_pid(timeout_seconds: float = 1.0) -> int | None:
    services = get_services()
    try:
        value = services._daemon_probe(timeout=min(1.0, max(0.0, timeout_seconds)))
    except (services.CliError, ipc_errors.TransientDaemonError, OSError):
        return None
    daemon = value.get("daemon") if isinstance(value, dict) else None
    source = daemon if isinstance(daemon, dict) else value
    pid = source.get("pid") if isinstance(source, dict) else None
    return pid if type(pid) is int and pid > 0 else None


def _service_manager():
    services = get_services()
    from hyprial.daemon import ServiceConfig, ServiceManager

    executable = shutil.which("hyprial") or sys.argv[0]

    def handoff() -> None:
        if _serving_daemon_pid() is not None:
            services._stop_daemon_gracefully()

    return ServiceManager(
        ServiceConfig(
            executable=Path(executable).expanduser().resolve(),
            home=Path.home(),
            hyprial_home=services._hyprial_home(),
            state_dir=services._state_dir(),
        ),
        platform=_service_platform(),
        handoff_standalone=handoff,
    )


def _status_payload(status: Any) -> JsonObject:
    raw = asdict(status) if hasattr(status, "__dataclass_fields__") else vars(status)
    return {
        "ok": True,
        "platform": raw["platform"],
        "unit": raw["unit"],
        "unitPath": str(raw["unit_path"]),
        "installed": raw["installed"],
        "loaded": raw["loaded"],
        "running": raw["running"],
        "pid": raw["pid"],
        "bootPersistent": raw["boot_persistent"],
        "changed": raw["changed"],
        "attributed": raw.get("attributed"),
        "templateMatches": raw.get("template_matches"),
        "servingPid": raw.get("serving_pid"),
        "supervision": raw.get("supervision", "unsupervised"),
        "relaunchCount": raw.get("relaunch_count"),
        "lastExitCode": raw.get("last_exit_code"),
        "ownershipBusy": raw.get("ownership_busy", False),
        "ownershipReason": raw.get("ownership_reason"),
        "backupPath": (
            str(raw["backup_path"]) if raw.get("backup_path") is not None else None
        ),
    }


def _run_service(operation):  # type: ignore[no-untyped-def]
    services = get_services()
    try:
        return operation(_service_manager())
    except ServicePlatformUnsupported as error:
        raise services.CliError(
            "SERVICE_PLATFORM_UNSUPPORTED",
            str(error),
            {"platform": error.platform},
        ) from error
    except RuntimeError as error:
        from hyprial.daemon import ServiceOwnershipError

        if isinstance(error, ServiceOwnershipError):
            raise services.CliError(
                "SERVICE_OWNERSHIP_REFUSED",
                str(error),
                {"ownershipReason": error.reason},
            ) from error
        raise services.CliError(
            "SERVICE_MANAGER_FAILED",
            str(error),
            {"reason": _manager_failure_reason(error)},
        ) from error


def _install_status(manager: Any) -> JsonObject:
    installed = manager.install()
    observed = manager.wait_for_custody(_serving_daemon_pid)
    if hasattr(observed, "__dataclass_fields__"):
        observed = replace(observed, changed=installed.changed)
    payload = _status_payload(observed)
    if observed.supervision != "supervised":
        services = get_services()
        raise services.CliError(
            "SERVICE_SUPERVISION_LOST",
            "service was installed but did not take custody of the serving daemon",
            payload,
        )
    return payload


def _confirmation(action: str, *, yes: bool, json_output: bool) -> None:
    services = get_services()
    if yes:
        return
    if json_output or not _service_is_interactive():
        raise services.CliError(
            "CONFIRMATION_REQUIRED",
            f"daemon service {action} requires --yes in non-interactive mode",
        )
    if not confirm(f"{action.title()} the Hyprial daemon user service?"):
        raise services.CliError("OPERATION_DECLINED", f"service {action} declined")


@daemon_service_app.command("status")
def daemon_service_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Inspect service ownership, rendered configuration, and process custody."""

    services = get_services()
    services.runtime.execute(
        lambda: CliResult(
            _run_service(
                lambda manager: _status_payload(
                    manager.inspect(serving_pid=_serving_daemon_pid())
                )
            ),
            render=render_generic,
        ),
        json_output=json_output,
    )


@daemon_service_app.command("install")
def daemon_service_install(
    yes: bool = typer.Option(False, "--yes", help="Confirm service installation."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Install and bootstrap the owned per-user service."""

    services = get_services()

    def install(manager: Any) -> JsonObject:
        _confirmation("install", yes=yes, json_output=json_output)
        return _install_status(manager)

    def operation() -> CliResult:
        return CliResult(_run_service(install), render=render_generic)

    services.runtime.execute(operation, json_output=json_output)


@daemon_service_app.command("uninstall")
def daemon_service_uninstall(
    yes: bool = typer.Option(False, "--yes", help="Confirm service removal."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Unload and remove only an attributable Hyprial service file."""

    services = get_services()

    def uninstall(manager: Any) -> JsonObject:
        _confirmation("uninstall", yes=yes, json_output=json_output)
        return _status_payload(manager.uninstall())

    def operation() -> CliResult:
        return CliResult(_run_service(uninstall), render=render_generic)

    services.runtime.execute(operation, json_output=json_output)


def _manager_failure_reason(error: BaseException) -> str:
    reason = getattr(error, "reason", None)
    if isinstance(reason, str):
        return reason
    if isinstance(error, subprocess.TimeoutExpired):
        return "manager-command-timeout"
    if isinstance(error, FileNotFoundError):
        return "manager-command-unavailable"
    return "manager-command-failed"


def _lost_restart_data(status: Any, reason: str) -> JsonObject:
    return {
        "restart": {"supervision": "lost", "reason": reason},
        **_status_payload(status),
    }


def restart_supervised_daemon(
    before_pid: int | None,
    *,
    timeout_seconds: float | None = None,
):
    """Restart through an attributable supervisor, or return None when unmanaged.

    Upgrade passes the shared startup hard cap explicitly. Other service
    callers retain the manager's ordinary custody-poll default.
    """

    services = get_services()
    try:
        manager = _service_manager()
    except ServicePlatformUnsupported:
        return None
    try:
        before = manager.inspect(serving_pid=before_pid)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        reason = _manager_failure_reason(error)
        raise services.CliError(
            "SERVICE_SUPERVISION_LOST",
            f"could not inspect the configured daemon supervisor: {error}",
            {"restart": {"supervision": "lost", "reason": reason}},
        ) from error
    if not before.installed:
        if before.loaded:
            reason = "supervisor-file-missing"
            raise services.CliError(
                "SERVICE_SUPERVISION_LOST",
                "the daemon supervisor is loaded but its service file is missing",
                _lost_restart_data(before, reason),
            )
        return None
    ownership_reason = getattr(before, "ownership_reason", None)
    if before.attributed is not True:
        reason = ownership_reason or "service-file-unattributed"
        raise services.CliError(
            "SERVICE_OWNERSHIP_REFUSED",
            "refusing to restart through an unattributed or unsafe supervisor file",
            _lost_restart_data(before, reason),
        )
    if before.loaded is not True:
        if before_pid is None and _serving_daemon_pid() is None:
            # ``daemon stop`` / the login identity switch stop through the
            # supervisor (``launchctl bootout`` unloads the LaunchAgent).  A
            # restart of that stopped, owned service is a supervisor start --
            # the same bootstrap ``daemon start`` uses -- not lost custody.
            from hyprial.shell.impl.cli.commands.common.support import (
                DAEMON_START_IDLE_BUDGET_SECONDS,
            )

            return start_supervised_daemon(
                (manager, before),
                idle_budget=(
                    DAEMON_START_IDLE_BUDGET_SECONDS
                    if timeout_seconds is None
                    else timeout_seconds
                ),
            )
        raise services.CliError(
            "SERVICE_SUPERVISION_LOST",
            "the attributable daemon service is installed but not loaded",
            _lost_restart_data(before, "supervisor-not-loaded"),
        )
    if before_pid is not None and before.supervision != "supervised":
        raise services.CliError(
            "SERVICE_SUPERVISION_LOST",
            "the serving daemon pid is outside supervisor custody",
            _lost_restart_data(before, "supervisor-custody-mismatch"),
        )
    restart_deadline = (
        services.time.monotonic() + max(0.0, timeout_seconds)
        if timeout_seconds is not None
        else None
    )
    try:
        after = (
            manager.restart(_serving_daemon_pid)
            if timeout_seconds is None
            else manager.restart(
                _serving_daemon_pid,
                timeout_seconds=timeout_seconds,
            )
        )
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        reason = _manager_failure_reason(error)
        if timeout_seconds is not None and reason == "supervisor-start-hard-cap":
            raise services.CliError(
                ipc_errors.DAEMON_START_TIMEOUT,
                "supervised daemon did not become ready within the hard cap of "
                f"{timeout_seconds:g}s",
                _lost_restart_data(before, reason),
            ) from error
        raise services.CliError(
            "SERVICE_SUPERVISION_LOST",
            f"daemon supervisor restart failed: {error}",
            {
                "restart": {"supervision": "lost", "reason": reason},
                **_status_payload(before),
            },
        ) from error
    if after.running is False:
        status = after.last_exit_code
        suffix = f" with status {status}" if isinstance(status, int) else ""
        raise services.CliError(
            ipc_errors.DAEMON_START_FAILED,
            f"supervised daemon exited during startup{suffix}",
            _lost_restart_data(after, "supervisor-child-exited"),
        )
    probe_timeout = 2.0
    if restart_deadline is not None:
        probe_timeout = min(2.0, max(0.0, restart_deadline - services.time.monotonic()))
        if probe_timeout <= 0:
            raise services.CliError(
                ipc_errors.DAEMON_START_TIMEOUT,
                "supervised daemon did not become ready within the hard cap of "
                f"{timeout_seconds:g}s",
                _lost_restart_data(after, "supervisor-start-hard-cap"),
            )
    try:
        probe = services._daemon_probe(timeout=probe_timeout)
    except (services.CliError, ipc_errors.TransientDaemonError, OSError) as error:
        if timeout_seconds is None:
            raise services.CliError(
                "SERVICE_SUPERVISION_LOST",
                f"could not verify the restarted daemon: {error}",
                _lost_restart_data(after, "daemon-probe-failed"),
            ) from error
        raise services.CliError(
            ipc_errors.DAEMON_START_TIMEOUT,
            "supervised daemon did not become ready within the hard cap of "
            f"{timeout_seconds:g}s",
            _lost_restart_data(after, "supervisor-start-hard-cap"),
        ) from error
    launched = DaemonLaunchResult.existing(probe)
    # Let the one restart-verdict function diagnose an answering old daemon.
    # Turning this into generic custody loss here would hide the decisive
    # unchanged pid/epoch facts from the upgrade result and owner alert.
    if before_pid is not None and launched.pid == before_pid:
        return launched
    fresh_job = (
        isinstance(after.pid, int)
        and after.pid > 0
        and after.pid != before.pid
        and (before_pid is None or after.pid != before_pid)
    )
    exact_job = after.serving_pid == after.pid
    if after.supervision != "supervised" or not fresh_job or not exact_job:
        raise services.CliError(
            "SERVICE_SUPERVISION_LOST",
            "supervisor did not produce a fresh job pid matching the serving daemon",
            _lost_restart_data(after, "supervisor-restart-custody-mismatch"),
        )
    if launched.pid != after.pid:
        raise services.CliError(
            "SERVICE_SUPERVISION_LOST",
            "serving daemon changed after supervisor pid verification",
            _lost_restart_data(after, "daemon-probe-pid-mismatch"),
        )
    return launched


# --- supervisor-routed start/stop ---------------------------------------------
#
# With the per-user service installed, launchd (``KeepAlive``) / systemd
# (``Restart=always``) relaunch the daemon within seconds of any exit.  A CLI
# spawn next to it is a second launcher racing for the same locks, and an IPC
# shutdown alone is undone by the supervisor.  So start and stop go through the
# supervisor itself; an uninstalled service keeps the CLI spawn / IPC stop.

# The unit templates give the daemon ``SUPERVISOR_EXIT_TIMEOUT_SECONDS``
# (launchd ``ExitTimeOut``, systemd ``TimeoutStopSec``) between SIGTERM and
# SIGKILL: at least the daemon's own close budget + exit backstop
# (``TEARDOWN_BUDGETED_SECONDS``) plus margin, so a supervisor stop never cuts
# the sqlite / journal / log-writer drain short.  The CLI waits a little
# longer than the supervisor's own kill deadline.
SUPERVISOR_EXIT_TIMEOUT_SECONDS = 120
SUPERVISOR_STOP_TIMEOUT_SECONDS = SUPERVISOR_EXIT_TIMEOUT_SECONDS + 30.0


def installed_supervisor() -> tuple[Any, Any] | None:
    """``(manager, status)`` for an attributable installed service, else None."""

    try:
        manager = _service_manager()
    except ServicePlatformUnsupported:
        return None
    if not manager.unit_path.is_file():
        return None
    try:
        status = manager.inspect()
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return None
    if not status.installed or status.attributed is not True:
        return None
    return manager, status


def _supervisor_command(manager: Any, command: tuple[str, ...], *, timeout: float) -> None:
    services = get_services()
    try:
        result = manager.runner(command, timeout=timeout)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        raise services.CliError(
            "SERVICE_MANAGER_FAILED",
            f"daemon supervisor command failed: {' '.join(command)}: {error}",
            {"reason": _manager_failure_reason(error), "command": list(command)},
        ) from error
    if result.returncode != 0:
        raise services.CliError(
            "SERVICE_MANAGER_FAILED",
            f"daemon supervisor command failed ({result.returncode}): "
            f"{' '.join(command)}: {(result.stderr or '').strip()}",
            {"reason": "manager-command-failed", "command": list(command)},
        )


def request_supervised_start(supervisor: tuple[Any, Any]) -> None:
    """Issue the supervisor's start command without waiting for readiness."""

    manager, status = supervisor
    if manager.platform == "launchd":
        command: tuple[str, ...] = (
            ("launchctl", "kickstart", f"gui/{manager.uid}/{status.unit}")
            if status.loaded
            else ("launchctl", "bootstrap", f"gui/{manager.uid}", str(status.unit_path))
        )
    else:
        command = ("systemctl", "--user", "start", "hyprial.service")
    _supervisor_command(manager, command, timeout=15.0)


def start_supervised_daemon(
    supervisor: tuple[Any, Any],
    *,
    idle_budget: float,
    endpoints_requested: bool = False,
    startup_progress: Any = None,
) -> DaemonLaunchResult:
    """Ask the installed supervisor to start the daemon, then wait for it."""

    services = get_services()
    manager, status = supervisor
    try:
        probe = services._daemon_probe(timeout=0.5)
    except ipc_errors.DaemonUnavailableError:
        pass
    else:
        if endpoints_requested:
            raise services.CliError(
                "DAEMON_ALREADY_RUNNING",
                "stop the running daemon before changing zenoh endpoints",
            )
        return DaemonLaunchResult.existing(probe)
    if endpoints_requested:
        raise services.CliError(
            "SERVICE_MANAGED_ENDPOINTS",
            "--listen/--connect apply to a CLI-spawned daemon only, and the "
            "installed per-user service launches this daemon: set "
            "HYPRIAL_ZENOH_LISTEN / HYPRIAL_ZENOH_CONNECT in the service "
            "environment, or uninstall the service first",
            {"platform": status.platform, "unitPath": str(status.unit_path)},
        )
    from datetime import UTC, datetime

    state_dir = services._state_dir()
    spawned_at = datetime.now(UTC)
    request_supervised_start(supervisor)
    ready = services._wait_for_daemon(
        timeout=idle_budget,
        startup_progress=(
            None
            if startup_progress is None
            else lambda: startup_progress(state_dir, spawned_at=spawned_at)
        ),
    )
    return DaemonLaunchResult.launched(ready)


def stop_supervised_daemon(
    supervisor: tuple[Any, Any],
    *,
    expected_epoch: str | None = None,
    require_known_pid: bool = False,
    read_identity: Callable[[int], str | None] | None = None,
) -> JsonObject:
    """Stop through the supervisor so it does not relaunch the daemon.

    Returns the same facts an IPC ``shutdown`` answer carries (``pid``,
    ``epoch``, ``stateDir``, ``lockPath``) so the caller's teardown/exit
    proof is unchanged.  The unit stays installed: the next ``daemon start``
    (or login) starts it again through the supervisor.

    Every refusal happens before the supervisor is asked: an epoch mismatch,
    or (``require_known_pid``, the identity switch) a probe that named no
    pid.  The old process identity is read *before* the stop command, while
    the process still exists (``read_identity``), and returned as
    ``processIdentity`` so the
    caller's PID-reuse fence has something to compare against.
    """

    services = get_services()
    manager, status = supervisor
    try:
        value = services._daemon_probe(timeout=1.0)
    except (services.CliError, ipc_errors.TransientDaemonError, OSError):
        value = None
    daemon = value.get("daemon") if isinstance(value, dict) else None
    source = daemon if isinstance(daemon, dict) else value
    source = source if isinstance(source, dict) else {}
    pid = source.get("pid") if type(source.get("pid")) is int else None
    epoch = source.get("epoch") if isinstance(source.get("epoch"), str) else None
    if expected_epoch is not None and epoch != expected_epoch:
        raise services.CliError(
            "DAEMON_EPOCH_MISMATCH",
            "shutdown plan belongs to another daemon epoch",
        )
    if require_known_pid and (pid is None or pid <= 0):
        raise services.CliError(
            "DAEMON_STOP_IDENTITY_UNKNOWN",
            "daemon shutdown did not identify the old pid; refusing to commit "
            "an identity change without an exit fence",
            {"oldPid": None, "teardownCompleted": False},
        )
    process_identity: str | None = None
    if pid is not None and pid > 0 and read_identity is not None:
        process_identity = read_identity(pid)
    if manager.platform == "launchd":
        if status.loaded:
            _supervisor_command(
                manager,
                ("launchctl", "bootout", f"gui/{manager.uid}/{status.unit}"),
                timeout=SUPERVISOR_STOP_TIMEOUT_SECONDS,
            )
    else:
        _supervisor_command(
            manager,
            ("systemctl", "--user", "stop", "hyprial.service"),
            timeout=SUPERVISOR_STOP_TIMEOUT_SECONDS,
        )
    state_dir = Path(services._state_dir())
    return {
        "ok": True,
        "stopping": pid is not None,
        "pid": pid if pid is not None and pid > 0 else None,
        "epoch": epoch,
        "stateDir": str(state_dir),
        "lockPath": str(state_dir / "daemon.lock"),
        "processIdentity": process_identity,
        "supervisor": {"platform": manager.platform, "unit": status.unit},
    }


def _stop_stray_daemon(expected_pid: int) -> None:
    services = get_services()
    actual = _serving_daemon_pid()
    if actual != expected_pid:
        raise services.CliError(
            "SERVICE_PID_CHANGED",
            f"serving daemon changed from {expected_pid} to {actual}; refusing repair",
        )
    services._stop_daemon_gracefully()


def daemon_service_doctor_check(
    *, serving_pid: int | None, fix: bool
) -> JsonObject | None:
    """Diagnose and optionally repair service template and custody drift."""

    try:
        manager = _service_manager()
    except ServicePlatformUnsupported:
        return None
    try:
        status = (
            manager.reconcile(_serving_daemon_pid, stop_stray=_stop_stray_daemon)
            if fix
            else manager.inspect(serving_pid=serving_pid)
        )
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        from hyprial.daemon import (
            USER_BUS_ADDRESS_UNREACHABLE_REASON,
            USER_BUS_UNAVAILABLE_REASON,
        )
        reason = getattr(error, "reason", None)
        missing_file = reason == "supervisor-file-missing"
        bus_unavailable = reason == USER_BUS_UNAVAILABLE_REASON
        bus_address_unreachable = reason == USER_BUS_ADDRESS_UNREACHABLE_REASON
        if bus_unavailable:
            # "cannot ask" is not "asked and it is broken": report the fourth
            # state so the aggregated view cannot count it as a pass.
            return {
                "name": "daemon-service",
                "status": "unknown",
                "detail": str(error),
                "action": {
                    "command": "loginctl enable-linger <user>",
                    "description": (
                        "This session cannot reach the per-user systemd bus, so the "
                        "service cannot be inspected. Enable lingering for the daemon "
                        "account, then retry with XDG_RUNTIME_DIR=/run/user/<uid>."
                    ),
                },
                "metrics": {"reason": reason},
            }
        if bus_address_unreachable:
            # Same reasoning as ``bus_unavailable``: the supervisor was never
            # asked (the address points nowhere), so this is "cannot ask", not
            # "asked and broken".  #2145 documented that in the contract table
            # but only wired the no-session case to ``unknown``.
            return {
                "name": "daemon-service",
                "status": "unknown",
                "detail": str(error),
                "action": {
                    "command": "env -u DBUS_SESSION_BUS_ADDRESS hyprial doctor",
                    "description": (
                        "DBUS_SESSION_BUS_ADDRESS is set but its socket is missing, so "
                        "the service cannot be inspected. Fix or unset it; lingering "
                        "does not help while the address is explicit."
                    ),
                },
                "metrics": {"reason": reason},
            }
        if missing_file:
            description = (
                "Manually unload the stale supervisor label, then reinstall the service."
            )
        else:
            description = "Inspect ownership before changing the supervisor file."
        return {
            "name": "daemon-service",
            "status": "fail",
            "detail": str(error),
            "action": {
                "command": "hyprial daemon service status --json",
                "description": description,
            },
            "metrics": {"reason": reason} if isinstance(reason, str) else {},
        }
    payload = _status_payload(status)
    metrics = {key: value for key, value in payload.items() if key != "ok"}
    failures: list[str] = []
    missing_loaded_file = status.loaded and not status.installed
    if missing_loaded_file:
        failures.append("the loaded supervisor service file is missing")
    if status.installed and status.attributed is not True:
        failures.append("the installed supervisor file is not attributable to Hyprial")
    if status.installed and status.template_matches is not True:
        failures.append(
            "the installed supervisor file differs from the rendered template"
        )
    if status.installed and status.supervision == "lost":
        failures.append("the serving daemon is outside supervisor custody")
    if status.ownership_busy and (status.relaunch_count or 0) > 1:
        failures.append("the supervisor is relaunching after DAEMON_OWNERSHIP_BUSY")
    if failures:
        description = (
            "Manually unload the stale supervisor label, then reinstall the service."
            if missing_loaded_file
            else "Reconcile the owned service and verify serving process custody."
        )
        return {
            "name": "daemon-service",
            "status": "fail",
            "detail": "; ".join(failures),
            "action": {
                "command": (
                    "hyprial daemon service status --json"
                    if missing_loaded_file
                    else "hyprial doctor --fix"
                ),
                "description": description,
            },
            "metrics": metrics,
        }
    if not status.installed:
        # Never installed => the daemon runs unsupervised.  That is not a fault
        # (many hosts start it by hand) and must not turn a healthy machine red,
        # so it is a warn: it degrades restart resilience, nothing else.
        return {
            "name": "daemon-service",
            "status": "warn",
            "detail": (
                "no supervisor service is installed: the daemon runs unsupervised "
                "and will not restart after a reboot"
            ),
            "action": {
                "command": "hyprial daemon service install",
                "description": (
                    "Install the per-user service so the daemon restarts at login "
                    "and after failures (on systemd, enable lingering too)."
                ),
            },
            "metrics": {
                **metrics,
                "reason": "unsupervised",
                "bootPersistent": status.boot_persistent,
            },
        }
    if status.platform == "systemd" and status.boot_persistent is not True:
        return {
            "name": "daemon-service",
            "status": "warn",
            "detail": (
                "the service is installed but user lingering is off: it starts at "
                "login, not at boot"
            ),
            "action": {
                "command": "loginctl enable-linger <user>",
                "description": "Enable lingering so the user manager (and the "
                "service) starts without a login session.",
            },
            "metrics": {**metrics, "reason": "linger-off"},
        }
    detail = (
        f"{status.platform} service matches its template and custody is "
        f"{status.supervision}"
    )
    return {
        "name": "daemon-service",
        "status": "ok",
        "detail": detail,
        "metrics": metrics,
    }


daemon_app.add_typer(daemon_service_app, name="service")
