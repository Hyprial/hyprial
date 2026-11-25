"""Doctor check for daemon upgrade steps and live legacy argv."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.support import request_upgrade_steps as request_steps
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject


def _upgrade_steps_doctor_check(*, fix: bool) -> JsonObject:
    """List/rerun the daemon upgrade steps; never execute on a plain read."""

    services = get_services()
    status = request_steps(services, apply=True) if fix else None
    if status is None or not status.get("unavailable"):
        status = request_steps(services)
    malformed = not isinstance(status.get("steps"), list) or not isinstance(
        status.get("pending"), list
    )
    if status.get("unavailable") or malformed:
        return {"name": "upgrade-steps", "status": "warn",
                "detail": "daemon upgrade status unavailable; no local migration was run",
                "relaunchRequired": False, "steps": [], "legacyArgvFiles": [],
                "legacyArgvProcesses": []}
    waiting = {
        row["id"] for row in status.get("steps", [])
        if row.get("state") == "applied_unconfirmed"
        and not any((row.get("data") or {}).get(key) for key in ("pending", "failed", "conflicts"))
    }
    pending = [item for item in status.get("pending", []) if item not in waiting]
    legacy_files = status.get("legacyArgvFiles", [])
    legacy_processes_live = status.get("legacyProcesses", [])
    if pending or legacy_files or legacy_processes_live:
        detail = []
        if pending:
            states = {row["id"]: row.get("state", "pending") for row in status.get("steps", [])}
            detail.append("pending upgrade steps: " + ", ".join(
                f"{item} ({states.get(item, 'pending')})" for item in pending))
        if legacy_files:
            detail.append(f"legacy argv files: {len(legacy_files)}")
        if legacy_processes_live:
            detail.append(
                "legacy argv processes require relaunch: "
                + ", ".join(str(item.get("pid")) for item in legacy_processes_live)
            )
        return {
            "name": "upgrade-steps",
            "status": "warn",
            "detail": "; ".join(detail),
            "relaunchRequired": bool(legacy_processes_live),
            "action": {
                "command": "hyprial doctor --fix",
                "description": "Run pending migration steps, then relaunch agent sessions.",
            },
            "steps": status["steps"],
            "legacyArgvFiles": legacy_files,
            "legacyArgvProcesses": legacy_processes_live,
        }
    return {
        "name": "upgrade-steps",
        "status": "ok",
        "detail": ("receiver set (applied_unconfirmed); will be confirmed when another device syncs"
                   if waiting else "all upgrade steps are complete"),
        "relaunchRequired": False,
        "steps": status["steps"],
        "legacyArgvFiles": [],
        "legacyArgvProcesses": [],
    }

