"""Doctor row for the owner's user agent on this machine (UA1c).

The daemon reports a ``userAgent`` block on ``ps``.  A daemon old enough not
to report it gets no row.  ``present``, ``pending`` (the ensure has not
finished yet after a start) and ``no-owner`` are not problems.  Every other
state means this machine has no usable user agent: its name is held by an
ordinary agent, or every create attempt failed.  The row is a
warning: the daemon serves, only the owner's identity agent on this machine
is missing.
"""
from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.support import JsonObject

_QUIET = frozenset({"present", "pending", "no-owner"})
_ACTIONS = {
    "name-taken": (
        "hyprial agent list",
        "An ordinary agent holds the reserved name hyprial-user.  The daemon never "
        "adopts it, and a rename keeps its internal name.  Destroying that agent "
        "frees the name; the daemon then creates the user agent at its next start.",
    ),
    "unavailable": (
        "hyprial daemon restart",
        "Every create attempt failed; read user_agent.create_failed in the daemon "
        "log, fix the cause and restart the daemon.",
    ),
}
_DEFAULT_ACTION = (
    "hyprial daemon status --json",
    "Inspect userAgent in the daemon status and the user_agent events in the daemon log.",
)


def user_agent_doctor_check(result: JsonObject) -> JsonObject | None:
    """One row for the ``userAgent`` block of ``ps``; None for an older daemon."""

    info = result.get("userAgent")
    if not isinstance(info, dict):
        return None
    state = str(info.get("state"))
    if state in _QUIET:
        address = info.get("address")
        detail = (
            f"the owner's user agent is {address}" if state == "present"
            else f"the owner's user agent is {state}"
        )
        return {"name": "user-agent", "status": "ok", "detail": detail}
    command, description = _ACTIONS.get(state, _DEFAULT_ACTION)
    return {
        "name": "user-agent",
        "status": "warn",
        "detail": f"this machine has no usable user agent ({state}): {info.get('reason')}",
        "metrics": dict(info),
        "action": {"command": command, "description": description},
    }
