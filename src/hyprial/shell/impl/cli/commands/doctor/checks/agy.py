"""Doctor check for registered Antigravity (agy) headless connectors."""

from __future__ import annotations

import shutil

from hyprial.shell.impl.cli.commands.common.support import JsonObject


def agy_doctor_check(result: JsonObject) -> JsonObject | None:
    """Verify the agy binary is available on PATH when agy connectors exist."""

    raw_connectors = result.get("connectors")
    if not isinstance(raw_connectors, list):
        return None
    connectors = [
        item
        for item in raw_connectors
        if isinstance(item, dict) and item.get("runtime") == "agy"
    ]
    if not connectors:
        return None
    binary = shutil.which("agy")
    if binary is None:
        return {
            "name": "agy",
            "status": "fail",
            "detail": "agy binary not found on PATH while agy connector(s) are registered",
            "action": {
                "command": "which agy",
                "description": (
                    "Install the Antigravity CLI (agy) or fix PATH before "
                    "restarting agy workers."
                ),
            },
            "metrics": {"connectors": len(connectors)},
        }
    return {
        "name": "agy",
        "status": "ok",
        "detail": f"agy binary available at {binary} ({len(connectors)} connector(s))",
        "metrics": {"connectors": len(connectors)},
    }


def agy_doctor_checks(result: JsonObject) -> list[JsonObject]:
    """List form for `checks.extend`: empty when no agy connector is active."""

    check = agy_doctor_check(result)
    return [] if check is None else [check]
