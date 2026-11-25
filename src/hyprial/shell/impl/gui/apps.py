"""Retired paired-GUI upgrade placeholder that ``hyprial upgrade`` still records."""

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


def upgrade(home: Path, *, check_only: bool = False, force: bool = False) -> dict[str, Any]:
    """A future GUI migration will restore the paired-component upgrade path."""
    _unavailable()
