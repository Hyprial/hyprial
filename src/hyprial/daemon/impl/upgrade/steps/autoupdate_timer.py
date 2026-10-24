"""Step 2: retire only the legacy autoupdate timer units."""

from __future__ import annotations

import plistlib
import shlex
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.autoupdate import (
    AUTOUPDATE_LABEL,
    SYSTEMD_SERVICE_UNIT,
    SYSTEMD_TIMER_UNIT,
    canonical_timer_home,
    legacy_timer_path_problem,
    remove_legacy_timer_units,
)

from . import UpgradeContext, UpgradeStep


def _legacy_argv(argv: list[str]) -> bool:
    if not argv or Path(argv[0]).name != "hyprial":
        return False
    tail = argv[1:]
    return tuple(tail) in {
        ("autoupdate", "run"),
        ("autoupdate", "run", "--json"),
        ("upgrade", "run"),
        ("upgrade", "run", "--json"),
        ("daemon", "upgrade", "run"),
        ("daemon", "upgrade", "run", "--json"),
    }


def _malformed(path: Path) -> str | None:
    if path.is_symlink():
        return "symlink refused at the exact timer path"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        return f"unreadable: {error}"
    if path.suffix == ".plist":
        try:
            value = plistlib.loads(text.encode("utf-8"))
        except Exception as error:  # noqa: BLE001 - malformed input is a finding
            return f"malformed plist: {error}"
        if not isinstance(value, dict) or value.get("Label") != AUTOUPDATE_LABEL:
            return "malformed plist: autoupdate label missing"
        argv = value.get("ProgramArguments")
        if not isinstance(argv, list) or not all(
            isinstance(item, str) for item in argv
        ):
            return "malformed plist: ProgramArguments missing"
        if not _legacy_argv(argv):
            return "malformed plist: not a retired autoupdate argv"
        return None
    if not text.strip().startswith("[Unit]"):
        return "malformed systemd unit"
    if path.name == SYSTEMD_SERVICE_UNIT:
        exec_starts = [
            line.removeprefix("ExecStart=")
            for line in text.splitlines()
            if line.startswith("ExecStart=")
        ]
        if len(exec_starts) != 1:
            return "malformed systemd service: ExecStart must appear exactly once"
        try:
            argv = shlex.split(exec_starts[0])
        except ValueError as error:
            return f"malformed systemd ExecStart: {error}"
        if not _legacy_argv(argv):
            return "malformed systemd service: not a retired autoupdate argv"
        return None
    units = [
        line.removeprefix("Unit=").strip()
        for line in text.splitlines()
        if line.startswith("Unit=")
    ]
    if len(units) != 1 or units[0] != SYSTEMD_SERVICE_UNIT:
        return "malformed systemd timer: service reference missing"
    return None


def _run(context: UpgradeContext) -> dict[str, Any]:
    home, home_problem = canonical_timer_home(Path(context.home))
    if home_problem is not None:
        return {"removed": [], "failed": [{"path": str(context.home), "error": home_problem}]}
    platform = context.platform
    if platform is None:
        platform = "launchd" if __import__("sys").platform == "darwin" else "systemd"
    if platform == "launchd":
        candidates = (
            home / "Library" / "LaunchAgents" / f"{AUTOUPDATE_LABEL}.plist",
        )
    else:
        unit_dir = home / ".config" / "systemd" / "user"
        candidates = (
            unit_dir / SYSTEMD_SERVICE_UNIT,
            unit_dir / SYSTEMD_TIMER_UNIT,
        )
    failed = []
    for path in candidates:
        if path.exists() or path.is_symlink():
            problem = legacy_timer_path_problem(path, home)
            if problem is None:
                problem = _malformed(path)
            if problem is not None:
                failed.append({"path": str(path), "error": problem})
    if failed:
        return {"removed": [], "failed": failed}
    removed = remove_legacy_timer_units(
        home=home,
        platform=platform,  # type: ignore[arg-type]
        runner=context.command_runner,
        uid=context.uid,
    )
    return {"removed": [str(path) for path in removed]}


STEP = UpgradeStep("autoupdate-timer-v1", _run)

__all__ = [
    "AUTOUPDATE_LABEL",
    "STEP",
    "SYSTEMD_SERVICE_UNIT",
    "SYSTEMD_TIMER_UNIT",
]
