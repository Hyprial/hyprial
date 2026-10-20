"""Doctor projection for launcher-owned dsh-tui carriers.

``channelConfirmed`` is not reported: the daemon sets it for every carrier
source at registration, so it can never disagree with ``total``. What can go
stale is the TUI process fence, so a fenced registration whose TUI pid is
gone or reused counts as ``processGone`` (positive evidence only; an
unreadable process is not counted).
"""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.support import JsonObject


def _dsh_tui_doctor_check(result: JsonObject) -> JsonObject | None:
    """Report interactive dsh-tui registrations without claiming wake health."""

    raw_sessions = result.get("interactiveSessions")
    if not isinstance(raw_sessions, list):
        return None
    sessions = [
        item
        for item in raw_sessions
        if isinstance(item, dict)
        and (
            item.get("source") == "dsh-channel"
            or item.get("runtime") == "dsh_interactive"
        )
    ]
    if not sessions:
        return None
    total = len(sessions)
    unfenced = sum(
        not isinstance(item.get("processPid"), int)
        or not isinstance(item.get("processIdentity"), str)
        or not item.get("processIdentity")
        for item in sessions
    )
    from hyprial.daemon import dsh_tui_process_gone

    process_gone = sum(
        isinstance(item.get("processPid"), int)
        and isinstance(item.get("processIdentity"), str)
        and bool(item.get("processIdentity"))
        and dsh_tui_process_gone(item["processPid"], item["processIdentity"])
        for item in sessions
    )
    online = sum(item.get("online") is True for item in sessions)
    metrics: JsonObject = {
        "total": total,
        "online": online,
        "unfenced": unfenced,
        "processGone": process_gone,
    }
    if unfenced or process_gone:
        problems = []
        if unfenced:
            problems.append("lack the TUI process fence")
        if process_gone:
            problems.append(
                "name a TUI process that is no longer running (or whose pid "
                "was reused)"
            )
        return {
            "name": "dsh-tui-carrier",
            "status": "warn",
            "detail": (
                "one or more interactive dsh-tui registrations "
                + " and ".join(problems)
                + "; wake readiness cannot be inferred from an online actor "
                "record alone"
            ),
            "action": {
                "command": "hyprial start dsh --name <name> [--resume <session-id>]",
                "description": (
                    "Restart the dsh-tui session through the hyprial launcher so "
                    "the tool-only MCP child and launch-scoped injection plugin "
                    "register with the TUI process fence."
                ),
            },
            "metrics": metrics,
        }
    return {
        "name": "dsh-tui-carrier",
        "status": "ok",
        "detail": (
            "interactive dsh-tui registrations report a live, fenced TUI "
            "process"
        ),
        "metrics": metrics,
    }
