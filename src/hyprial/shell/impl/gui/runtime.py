"""Retired GUI lifecycle surface; fail closed until the new GUI is migrated."""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

from .errors import GuiError

_UNAVAILABLE = (
    "Hyprial GUI is temporarily unavailable in this build; "
    "use the CLI for daemon, agent, and messaging."
)


def _unavailable() -> NoReturn:
    raise GuiError("GUI_UNAVAILABLE", _UNAVAILABLE)


def gui_status(
    hyprial_home: Path, app: str = "gui", *, component: str | None = None
) -> dict[str, Any]:
    """The legacy GUI lifecycle is retired; no status is fabricated."""
    _unavailable()


def start_gui_background(
    hyprial_home: Path, app: str = "gui", *, component: str | None = None
) -> dict[str, Any]:
    """The legacy GUI launcher is retired; a new GUI will ship separately."""
    _unavailable()


def stop_gui(
    hyprial_home: Path, app: str = "gui", *, component: str | None = None
) -> dict[str, Any]:
    """The legacy GUI stop path is retired with the rest of its lifecycle."""
    _unavailable()
