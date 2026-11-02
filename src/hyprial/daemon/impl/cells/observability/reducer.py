"""Pure run-state precedence; no I/O, callbacks, timers or policy actions."""
from .model import BaseRunState, ObservationHealth, RunCertainty, RunFacts, RunState
from .policy import RunPolicy


def derive_run_state(facts: RunFacts, policy: RunPolicy, *, now_ms: int, monotonic_ms: int) -> dict:
    state: RunState = "unknown"
    reason = "source-unavailable"
    certainty: RunCertainty = "unknown"
    base: BaseRunState = "in-turn" if facts.open_count else "idle"
    age = None
    suspicions = []
    health: ObservationHealth = "current"
    if facts.exit_verified:
        state, reason, certainty = "not-running", "process-exited", "observed"
    elif facts.operation is not None:
        state = facts.operation
        reason = "start-pending" if state == "starting" else "stop-pending"
        certainty = "observed"
    elif not facts.contact_current:
        reason, health = "carrier-stale", "stale"
    elif facts.conflict:
        reason, health = "conflicting-facts", "resync-required"
    elif facts.resync_required:
        reason, health = "observation-gap", "resync-required"
    elif not facts.known or not facts.running:
        health = "unavailable"
    elif facts.scope != "whole-session":
        reason, health = "partial-turn-coverage", "partial"
    else:
        certainty = "observed"
        if facts.open_count and facts.since_monotonic_ms is not None:
            age = max(0, monotonic_ms - facts.since_monotonic_ms)
        if facts.permission_supported and facts.waits:
            state, reason = "blocked", "permission-wait"
            if policy.permission_long_after_ms is not None and any(
                since is not None and monotonic_ms - since >= policy.permission_long_after_ms
                for _, since in facts.waits
            ):
                suspicions.append("permission-wait-long")
        elif facts.provider_block:
            state, reason = "blocked", "provider-block"
        else:
            if age is not None and policy.turn_long_after_ms is not None and age >= policy.turn_long_after_ms:
                suspicions.append("turn-over-budget")
            if facts.open_count and facts.activity_supported and facts.quiet:
                suspicions.append("producer-quiet")
            state = "suspected-stuck" if suspicions else base
            reason = suspicions[0] if suspicions else "turn-open" if facts.open_count else "no-open-turn"
            if suspicions:
                certainty = "inferred"
    if state not in {"suspected-stuck", "blocked"}:
        base = state
    return {
        "state": state, "baseState": base, "reason": reason, "certainty": certainty,
        "scope": facts.scope, "observedAtMs": now_ms,
        "sinceMs": facts.since_ms if state in {"in-turn", "suspected-stuck", "blocked"} else None,
        "turnAgeMs": age,
        "lastActivityAtMs": facts.last_activity_at_ms if facts.activity_supported and state != "unknown" else None,
        "coverage": {"turnBoundaries": facts.known, "permissionWait": facts.permission_supported,
                     "producerActivity": facts.activity_supported, "processExit": facts.process_exit_supported},
        "observationHealth": health, "suspicions": suspicions, "policyRevision": policy.revision,
        "providerBlocked": facts.provider_block,
        "thresholds": {"turnLongAfterMs": policy.turn_long_after_ms,
                       "permissionLongAfterMs": policy.permission_long_after_ms,
                       "quietAfterMs": facts.quiet_after_ms},
    }
