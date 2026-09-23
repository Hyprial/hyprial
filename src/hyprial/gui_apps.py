"""DSH GUI lifecycle and bounded retirement of the old Dashboard process."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from hyprial import gui_runtime as runtime
from hyprial.contracts import ipc_errors
from hyprial.installers import InstallError

APPS = ("dsh",)


def resolve_invocation(
    app_or_action: str = "start", action: str | None = None,
) -> tuple[str, str | None]:
    """Normalize GUI application subcommands before any lifecycle work."""
    if action is not None or app_or_action not in ("start", "status", "stop", "upgrade"):
        raise InstallError(
            ipc_errors.INVALID_ARGUMENT,
            "Use hyprial gui [start|status|stop|upgrade]; dashboard/dsh/all selectors are retired",
        )
    return app_or_action, None


def _selected(action: str, app: str | None) -> tuple[str, ...]:
    if app is not None:
        raise InstallError(ipc_errors.INVALID_ARGUMENT, "GUI application selectors are retired; use hyprial gui")
    return APPS


def _retire_dashboard(home: Path) -> None:
    """Stop only a verifiably owned legacy process; never delete its record."""
    if not (home / "apps/gui/runtimes/dashboard/process.json").exists():
        return
    state = runtime.gui_status(home, "gui", component="dashboard")
    if state["state"] == "unknown":
        raise InstallError("GUI_PROCESS_UNKNOWN", "Cannot verify retired Dashboard ownership; refusing replacement")
    if state["state"] == "running":
        runtime.stop_gui(home, "gui", component="dashboard")


def _each(home: Path, action: str, apps: tuple[str, ...]) -> dict[str, Any]:
    operation = {
        "start": runtime.start_gui_background,
        "status": runtime.gui_status,
        "stop": runtime.stop_gui,
    }[action]
    results = {}
    for app in apps:
        try:
            results[app] = {**operation(home, "gui", component=app), "app": app}
        except InstallError as error:
            results[app] = {
                "ok": False,
                "app": app,
                "error": {"code": error.code, "message": str(error)},
            }
    result = {
        "ok": all(value.get("ok") is True for value in results.values()),
        "name": "gui",
        "apps": results,
    }
    if not result["ok"]:
        raise InstallError(
            "GUI_APP_FAILED",
            "DSH GUI operation failed",
            result,
        )
    return result


def perform(home: Path, action: str, app: str | None = None) -> dict[str, Any]:
    if action not in ("start", "status", "stop"):
        raise InstallError(ipc_errors.INVALID_ARGUMENT, "Unsupported GUI action")
    selected = _selected(action, app)
    if action == "status":
        return _each(home, action, selected)
    # Serialize starts/stops with package upgrades, while keeping the existing
    # per-process identity locks. Distinct path avoids recursive flock.
    with runtime._lifecycle_lock(home / "apps" / "gui" / "lifecycle"):
        _retire_dashboard(home)
        return _each(home, action, selected)


def upgrade(home: Path, run_upgrade: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """Upgrade once; restore exactly those applications stopped by this call."""
    with runtime._lifecycle_lock(home / "apps" / "gui" / "lifecycle"):
        stopped: list[str] = []

        def before_apply() -> None:
            states = {
                app: runtime.gui_status(home, "gui", component=app) for app in APPS
            }
            if any(state["state"] == "unknown" for state in states.values()):
                raise InstallError(
                    "GUI_PROCESS_UNKNOWN",
                    "Cannot verify GUI process ownership; refusing package replacement",
                )
            _retire_dashboard(home)
            for app, state in states.items():
                if state["state"] == "running":
                    stopped.append(app)
                    runtime.stop_gui(home, "gui", component=app)

        result = None
        failure = None
        try:
            result = run_upgrade(before_apply)
        except BaseException as error:
            failure = error
        restored: dict[str, Any] = {}
        for app in stopped:
            try:
                restored[app] = {
                    **runtime.start_gui_background(home, "gui", component=app),
                    "app": app,
                }
            except Exception as error:
                restored[app] = {
                    "ok": False,
                    "app": app,
                    "error": {
                        "code": getattr(error, "code", "GUI_START_FAILED"),
                        "message": str(error),
                    },
                }
        if any(value.get("ok") is not True for value in restored.values()):
            raise InstallError(
                "GUI_RESTORE_FAILED",
                "GUI upgrade/restoration did not complete; inspect per-app results",
                {
                    "apps": restored,
                    "upgradeError": str(failure) if failure else None,
                },
            ) from failure
        if failure is not None:
            raise failure
        return {**(result or {}), "restarted": bool(stopped), "apps": restored}
