"""Doctor check for daemon upgrade steps and live legacy argv."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject


def _upgrade_steps_doctor_check(*, fix: bool) -> JsonObject:
    """List/rerun the daemon upgrade steps; never execute on a plain read."""

    services = get_services()
    from hyprial.daemon import legacy_processes, run_upgrade_steps, upgrade_step_status

    state_dir = services._state_dir()
    status = upgrade_step_status(state_dir)
    if fix:
        status["fix"] = run_upgrade_steps(state_dir, home=services._hyprial_home())
        status = upgrade_step_status(state_dir)
    pending = status.get("pending", [])
    legacy_files = status.get("legacyArgvFiles", [])
    legacy_processes_live = legacy_processes()
    if pending or legacy_files or legacy_processes_live:
        detail = []
        if pending:
            detail.append("pending upgrade steps: " + ", ".join(str(item) for item in pending))
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
        "detail": "all upgrade steps are complete",
        "relaunchRequired": False,
        "steps": status["steps"],
        "legacyArgvFiles": [],
        "legacyArgvProcesses": [],
    }

