"""Release files owned by one detached TUI launch."""

from __future__ import annotations

import shutil
from pathlib import Path


def skill_plugin_path(config_path: Path) -> Path:
    return config_path.with_name(f"{config_path.stem}-skills")


def cleanup_launch_resources(
    config_path: Path, recovery_path: Path | None = None
) -> None:
    """Idempotently remove files materialized for one interactive launch."""

    config_path.unlink(missing_ok=True)
    if recovery_path is not None:
        recovery_path.unlink(missing_ok=True)
    shutil.rmtree(skill_plugin_path(config_path), ignore_errors=True)
