"""User-service ownership for ``hyprial daemon service`` and doctor."""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

import typer

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.commands.daemon.commands import daemon_app
from hyprial.shell.impl.cli.output import CliResult, confirm, render_generic


daemon_service_app = typer.Typer(
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


def _serving_daemon_pid() -> int | None:
    services = get_services()
    try:
        value = services._daemon_probe(timeout=1.0)
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
    services._execute(
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

    services._execute(operation, json_output=json_output)


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

    services._execute(operation, json_output=json_output)


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


def restart_supervised_daemon(before_pid: int | None):
    """Restart through an attributable supervisor, or return None when unmanaged."""

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
    try:
        after = manager.restart(_serving_daemon_pid)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        reason = _manager_failure_reason(error)
        raise services.CliError(
            "SERVICE_SUPERVISION_LOST",
            f"daemon supervisor restart failed: {error}",
            {
                "restart": {"supervision": "lost", "reason": reason},
                **_status_payload(before),
            },
        ) from error
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
    try:
        probe = services._daemon_probe(timeout=2.0)
    except (services.CliError, ipc_errors.TransientDaemonError, OSError) as error:
        raise services.CliError(
            "SERVICE_SUPERVISION_LOST",
            f"could not verify the restarted daemon: {error}",
            _lost_restart_data(after, "daemon-probe-failed"),
        ) from error
    from hyprial.kernel import DaemonLaunchResult

    launched = DaemonLaunchResult.existing(probe)
    if launched.pid != after.pid:
        raise services.CliError(
            "SERVICE_SUPERVISION_LOST",
            "serving daemon changed after supervisor pid verification",
            _lost_restart_data(after, "daemon-probe-pid-mismatch"),
        )
    return launched


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
        reason = getattr(error, "reason", None)
        missing_file = reason == "supervisor-file-missing"
        return {
            "name": "daemon-service",
            "status": "fail",
            "detail": str(error),
            "action": {
                "command": "hyprial daemon service status --json",
                "description": (
                    "Manually unload the stale supervisor label, then reinstall the service."
                    if missing_file
                    else "Inspect ownership before changing the supervisor file."
                ),
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
        return None
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
