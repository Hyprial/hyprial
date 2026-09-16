"""Two independently owned GUI processes in one installed GUI package."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from hyprial import gui_runtime as runtime
from hyprial.contracts import ipc_errors
from hyprial.installers import InstallError

APPS = ("dashboard", "dsh")


def resolve_invocation(
    app_or_action: str = "start", action: str | None = None,
) -> tuple[str, str | None]:
    """Normalize GUI application subcommands before any lifecycle work."""
    if app_or_action in (*APPS, "all"):
        selected = app_or_action
        verb = action or "start"
    else:
        if action is not None:
            raise InstallError(ipc_errors.INVALID_ARGUMENT, "Use hyprial gui <dashboard|dsh|all> [start|status|stop]")
        selected = None
        verb = app_or_action
    if verb not in ("start", "status", "stop", "upgrade"):
        raise InstallError(ipc_errors.INVALID_ARGUMENT, "GUI action must be start, status, stop, or upgrade")
    if verb == "upgrade" and selected is not None:
        raise InstallError(ipc_errors.INVALID_ARGUMENT, "Upgrade the whole package with hyprial gui upgrade (no application selector)")
    return verb, selected


def _selected(action: str, app: str | None) -> tuple[str, ...]:
    if app is not None and app not in (*APPS, "all"):
        raise InstallError(ipc_errors.INVALID_ARGUMENT, "GUI application must be dashboard, dsh, or all")
    choice = app or ("all" if action == "status" else "dashboard")
    return APPS if choice == "all" else (choice,)


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
            "One or more GUI applications failed; other applications were left independent",
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
