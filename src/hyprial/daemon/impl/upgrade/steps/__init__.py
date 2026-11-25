"""One versioned registry of daemon upgrade steps.

A step is data plus an idempotent callable.  The runner owns marker writes,
locking and retry; step modules own only their migration and its result data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class UpgradeContext:
    """Inputs a step may need; state/home stay explicit for tests."""

    state_dir: Path
    home: Path
    command_runner: Callable[..., Any] | None = None
    process_runner: Callable[..., Any] | None = None
    platform: str | None = None
    uid: int | None = None
    hyprial_home: Path | None = None
    membership_migration: Callable[[], dict[str, Any]] | None = None


@dataclass(frozen=True)
class UpgradeStep:
    """A stable id and its idempotent migration."""

    id: str
    run: Callable[[UpgradeContext], dict[str, Any]]
    repeatable: bool = False


def _registry() -> tuple[UpgradeStep, ...]:
    from .autoupdate_timer import STEP as autoupdate_timer
    from .mcp_entry import STEP as mcp_entry
    from .org_membership import STEP as org_membership

    return (mcp_entry, autoupdate_timer, org_membership)


STEPS = _registry()

__all__ = ["STEPS", "UpgradeContext", "UpgradeStep"]
