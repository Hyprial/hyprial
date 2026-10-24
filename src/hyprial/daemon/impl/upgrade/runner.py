"""Locked, retryable execution of the versioned upgrade-step registry."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from hyprial.kernel import lock_exclusive, unlock

from .steps import STEPS, UpgradeContext, UpgradeStep


STEP_DIRNAME = "upgrade-steps"
LOCK_NAME = ".lock"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _step_dir(state_dir: Path) -> Path:
    path = Path(state_dir) / STEP_DIRNAME
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _marker_path(state_dir: Path, step_id: str) -> Path:
    return Path(state_dir) / STEP_DIRNAME / f"{step_id}.json"


def _read_marker(state_dir: Path, step_id: str) -> dict[str, Any] | None:
    path = _marker_path(state_dir, step_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_marker(state_dir: Path, step_id: str, value: dict[str, Any]) -> None:
    path = _marker_path(state_dir, step_id)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _row(state_dir: Path, step: UpgradeStep) -> dict[str, Any]:
    marker = _read_marker(state_dir, step.id)
    if marker is None:
        return {
            "id": step.id,
            "state": "pending",
            "attempts": 0,
            "updatedAt": None,
            "detail": None,
            "data": {},
        }
    return {
        "id": step.id,
        "state": str(marker.get("state", "pending")),
        "attempts": int(marker.get("attempts", 0)),
        "updatedAt": marker.get("updatedAt"),
        "detail": marker.get("detail"),
        "data": marker.get("data") if isinstance(marker.get("data"), dict) else {},
    }


def upgrade_step_status(
    state_dir: Path,
    *,
    steps: Iterable[UpgradeStep] | None = None,
) -> dict[str, Any]:
    selected = tuple(STEPS if steps is None else steps)
    rows = [_row(state_dir, step) for step in selected]
    from .steps.mcp_entry import scan_legacy_files

    return {
        "steps": rows,
        "pending": [row["id"] for row in rows if row["state"] != "done"],
        "legacyArgvFiles": scan_legacy_files(state_dir),
    }


def run_upgrade_steps(
    state_dir: Path,
    *,
    only: tuple[str, ...] | None = None,
    steps: Iterable[UpgradeStep] | None = None,
    home: Path | None = None,
    command_runner: Callable[..., Any] | None = None,
    process_runner: Callable[..., Any] | None = None,
    platform: str | None = None,
    uid: int | None = None,
) -> dict[str, Any]:
    """Run pending/failed steps under one lock; failures stay retryable."""

    selected = tuple(STEPS if steps is None else steps)
    if only is not None:
        selected = tuple(step for step in selected if step.id in only)
    directory = _step_dir(state_dir)
    context = UpgradeContext(
        state_dir=Path(state_dir),
        home=Path(home) if home is not None else Path.home(),
        command_runner=command_runner,
        process_runner=process_runner,
        platform=platform,
        uid=uid,
    )
    lock_path = directory / LOCK_NAME
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        lock_exclusive(lock.fileno())
        try:
            rows: list[dict[str, Any]] = []
            for step in selected:
                previous = _read_marker(state_dir, step.id)
                if (
                    previous is not None
                    and previous.get("state") == "done"
                    and not step.repeatable
                ):
                    rows.append(_row(state_dir, step))
                    continue
                attempts = int(previous.get("attempts", 0)) if previous else 0
                attempts += 1
                try:
                    data = step.run(context)
                    failed = data.get("failed") if isinstance(data, dict) else None
                    marker = {
                        "id": step.id,
                        "state": "failed" if failed else "done",
                        "attempts": attempts,
                        "updatedAt": _now(),
                        "detail": (
                            str(failed[0].get("error", "file migration failed"))
                            if failed
                            else None
                        ),
                        "data": data if isinstance(data, dict) else {},
                    }
                except Exception as error:  # noqa: BLE001 - a step must not block startup
                    marker = {
                        "id": step.id,
                        "state": "failed",
                        "attempts": attempts,
                        "updatedAt": _now(),
                        "detail": str(error),
                        "data": {},
                    }
                _write_marker(state_dir, step.id, marker)
                rows.append(_row(state_dir, step))
        finally:
            unlock(lock.fileno())
    return {
        "steps": rows,
        "pending": [row["id"] for row in rows if row["state"] != "done"],
    }


__all__ = [
    "STEP_DIRNAME",
    "run_upgrade_steps",
    "upgrade_step_status",
]
