"""CLI-compatible GUI entry points whose implementation is temporarily retired."""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

from hyprial.kernel import ipc_errors

from .errors import GuiError

APPS = ("gui",)
_UNAVAILABLE = (
    "Hyprial GUI is temporarily unavailable in this build; "
    "use the CLI for daemon, agent, and messaging."
)


def _unavailable() -> NoReturn:
    raise GuiError("GUI_UNAVAILABLE", _UNAVAILABLE)


def resolve_invocation(
    app_or_action: str = "start", action: str | None = None,
) -> tuple[str, str | None]:
    """Keep the public CLI grammar while the implementation is retired."""
    if action is not None or app_or_action not in ("start", "status", "stop", "upgrade"):
        raise GuiError(
            ipc_errors.INVALID_ARGUMENT,
            "Use hyprial gui [start|status|stop|upgrade]; dashboard/all selectors are retired",
        )
    return app_or_action, None


def perform(home: Path, action: str, app: str | None = None) -> dict[str, Any]:
    if action not in ("start", "status", "stop"):
        raise GuiError(ipc_errors.INVALID_ARGUMENT, "Unsupported GUI action")
    if app is not None:
        raise GuiError(ipc_errors.INVALID_ARGUMENT, "GUI application selectors are retired; use hyprial gui")
    _unavailable()


def upgrade(home: Path, *, check_only: bool = False, force: bool = False) -> dict[str, Any]:
    """A future GUI migration will restore the paired-component upgrade path."""
    _unavailable()
