"""Bundled GUI lifecycle and ownership-fenced legacy process retirement."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hyprial.kernel import ipc_errors

from . import runtime, unpack
from .errors import GuiError
from .product import product_bundle

APPS = ("gui",)


def resolve_invocation(
    app_or_action: str = "start", action: str | None = None,
) -> tuple[str, str | None]:
    """Normalize GUI application subcommands before any lifecycle work."""
    if action is not None or app_or_action not in ("start", "status", "stop", "upgrade"):
        raise GuiError(
            ipc_errors.INVALID_ARGUMENT,
            "Use hyprial gui [start|status|stop|upgrade]; dashboard/all selectors are retired",
        )
    return app_or_action, None


def _selected(action: str, app: str | None) -> tuple[str, ...]:
    if app is not None:
        raise GuiError(ipc_errors.INVALID_ARGUMENT, "GUI application selectors are retired; use hyprial gui")
    return APPS


def _retire_dashboard(home: Path) -> None:
    """Stop only a verifiably owned legacy process; never delete its record."""
    if not (home / "apps/gui/runtimes/dashboard/process.json").exists():
        return
    state = runtime.gui_status(home, "gui", component="dashboard")
    if state["state"] == "unknown":
        raise GuiError("GUI_PROCESS_UNKNOWN", "Cannot verify retired Dashboard ownership; refusing replacement")
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
        except GuiError as error:
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
        raise GuiError(
            "GUI_APP_FAILED",
            "Hyprial GUI operation failed",
            result,
        )
    return result


def perform(home: Path, action: str, app: str | None = None) -> dict[str, Any]:
    if action not in ("start", "status", "stop"):
        raise GuiError(ipc_errors.INVALID_ARGUMENT, "Unsupported GUI action")
    selected = _selected(action, app)
    if action == "status":
        return _each(home, action, selected)
    # Serialize starts/stops with bundle replacement, while keeping the
    # existing per-process identity locks. Distinct path avoids recursive flock.
    with runtime._lifecycle_lock(home / "apps" / "gui" / "lifecycle"):
        _retire_dashboard(home)
        return _each(home, action, selected)


def _is_current(home: Path, bundle: Any) -> bool:
    try:
        state = unpack.unpacked_state(home)
    except GuiError:
        return False
    return (
        state is not None
        and state.get("version") == bundle.version
        and state.get("sha256") == bundle.sha256
        and unpack.gui_source(home).is_dir()
    )


def upgrade(home: Path, *, check_only: bool = False, force: bool = False) -> dict[str, Any]:
    """Replace the unpacked GUI with the bundle paired with this product.

    Stops the running GUI only when a replacement will actually happen, and
    restarts exactly what this call stopped.
    """
    bundle = product_bundle()
    state = unpack.unpacked_state(home)
    installed_version = state.get("version") if state is not None else None
    current = _is_current(home, bundle)
    if check_only:
        return {
            "ok": True,
            "name": "gui",
            "checked": True,
            "installed": state is not None,
            "installedVersion": installed_version,
            "version": bundle.version,
            "commit": bundle.commit,
            "updateAvailable": not current,
            "upgraded": False,
            "restarted": False,
        }
    with runtime._lifecycle_lock(home / "apps" / "gui" / "lifecycle"):
        if current and not force:
            return {
                "ok": True,
                "name": "gui",
                "alreadyCurrent": True,
                "installed": True,
                "installedVersion": installed_version,
                "version": bundle.version,
                "commit": bundle.commit,
                "upgraded": False,
                "restarted": False,
            }
        status = runtime.gui_status(home, "gui", component="gui")
        if status["state"] == "unknown":
            raise GuiError(
                "GUI_PROCESS_UNKNOWN",
                "Cannot verify GUI process ownership; refusing bundle replacement",
            )
        was_running = status["state"] == "running"
        _retire_dashboard(home)
        if was_running:
            runtime.stop_gui(home, "gui", component="gui")
        failure: BaseException | None = None
        try:
            unpack.ensure_unpacked(home, bundle, force=True)
        except BaseException as error:  # noqa: BLE001 - the running GUI must be restored first
            failure = error
        if was_running:
            try:
                restarted = runtime.start_gui_background(home, "gui", component="gui")
            except Exception as error:
                raise GuiError(
                    "GUI_RESTORE_FAILED",
                    "GUI upgrade/restoration did not complete; inspect the GUI state",
                    {
                        "restoreError": str(error),
                        "upgradeError": str(failure) if failure else None,
                    },
                ) from failure or error
            if failure is not None:
                raise GuiError(
                    "GUI_APP_FAILED",
                    f"GUI bundle replacement failed; the previous GUI was restarted: {failure}",
                    {"restore": restarted},
                ) from failure
        if failure is not None:
            raise failure
        return {
            "ok": True,
            "name": "gui",
            "installed": True,
            "previousVersion": installed_version,
            "version": bundle.version,
            "commit": bundle.commit,
            "upgraded": True,
            "restarted": was_running,
        }
