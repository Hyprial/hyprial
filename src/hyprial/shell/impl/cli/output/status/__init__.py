"""Human cell diagnostics shared by agent list and top; no state derivation."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.shell.impl.cli.output.render import render_generic

_STATES = frozenset({
    "unknown", "starting", "idle", "in-turn", "blocked", "suspected-stuck",
    "stopping", "not-running",
})
_REASONS = frozenset({
    "source-unavailable", "process-exited", "start-pending", "stop-pending",
    "carrier-stale", "conflicting-facts", "observation-gap", "partial-turn-coverage",
    "permission-wait", "provider-block", "turn-over-budget", "producer-quiet",
    "turn-open", "no-open-turn",
})
_SUSPICIONS = frozenset({"turn-over-budget", "producer-quiet", "permission-wait-long"})


def _label(value: object, allowed: frozenset[str]) -> str:
    # A diagnostic label must never become a raw prompt/provider error renderer.
    return value if isinstance(value, str) and value in allowed else "unknown"


def _age(value: object) -> str:
    # Only the daemon's monotonic-derived duration is a measured turn age.
    # Neither lastKnown nor wall-time subtraction can fill in a missing age.
    if type(value) is not int or value < 0:
        return "-"
    seconds = value // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h{minutes % 60:02d}m"
    return f"{hours // 24}d{hours % 24:02d}h"


def format_run_state(value: object) -> str:
    """Format only supplied facts; absent fields do not imply idle or exit."""
    if not isinstance(value, Mapping):
        return "unknown (unsupported) turn-age=- permission=unknown"
    state = _label(value.get("state"), _STATES)
    reason = _label(value.get("reason"), _REASONS)
    base = _label(value.get("baseState"), _STATES - {"blocked", "suspected-stuck"})
    certainty = _label(value.get("certainty"), frozenset({"observed", "inferred", "unknown"}))
    scope = _label(value.get("scope"), frozenset({"whole-session", "carrier-deliveries"}))
    health = _label(value.get("observationHealth"), frozenset({
        "current", "stale", "unavailable", "partial", "resync-required",
    }))
    coverage = value.get("coverage")
    wait = coverage.get("permissionWait") if isinstance(coverage, Mapping) else None
    permission = "supported" if wait is True else "unsupported" if wait is False else "unknown"
    text = (
        f"{state} ({reason}) turn-age={_age(value.get('turnAgeMs'))} base={base}"
        f" certainty={certainty} scope={scope} health={health} permission={permission}"
    )
    if value.get("providerBlocked") is True:
        text += " provider-blocked=true"
    suspicions = value.get("suspicions")
    if isinstance(suspicions, list) and suspicions:
        # Stable deduplication also bounds unexpected future/invalid labels.
        text += " suspicions=" + ",".join(dict.fromkeys(
            _label(item, _SUSPICIONS) for item in suspicions
        ))
    return text


def agent_label(row: Mapping[str, Any]) -> str:
    """The human name of one agent row: ``display(a.<id>)``, or the display alone."""
    display = row.get("displayName") or row.get("actor") or "-"
    agent_id = row.get("agentId")
    return f"{display}({agent_id})" if isinstance(agent_id, str) and agent_id else str(display)


def render_agent_list(data: Mapping[str, Any]) -> str:
    """Keep the generic status detail, with a shared human-only run summary."""
    visible = dict(data)
    visible["agents"] = [
        {"agent": agent_label(row), **row, "runState": format_run_state(row.get("runState"))}
        for row in data["agents"]
    ]
    return render_generic(visible)


def render_agent_row(data: Mapping[str, Any]) -> str:
    """One agent answer (``agent rename``): the generic view headed by its label."""
    visible = dict(data)
    row = data.get("agent")
    if isinstance(row, Mapping):
        visible["agent"] = {"agent": agent_label(row), **row}
    return render_generic(visible)
