"""C: observations never execute lifecycle or business policy."""
from dataclasses import replace

import pytest

from hyprial.daemon.impl.cells.observability.model import RunFacts
from hyprial.daemon.impl.cells.observability.policy import RunPolicy, RunPolicies
from hyprial.daemon.impl.cells.observability.reducer import derive_run_state


def derive(**changes):
    facts = replace(RunFacts(source_epoch="runtime", known=True, running=True), **changes)
    return derive_run_state(facts, RunPolicy(), now_ms=2_000_000, monotonic_ms=2_000_000)


@pytest.mark.parametrize("changes,state,reason", [
    ({}, "idle", "no-open-turn"),
    ({"open_count": 1, "since_ms": 1, "since_monotonic_ms": 1}, "suspected-stuck", "turn-over-budget"),
    ({"open_count": 1}, "in-turn", "turn-open"),
    ({"exit_verified": True, "open_count": 1}, "not-running", "process-exited"),
    ({"exit_verified": True, "operation": "starting"}, "not-running", "process-exited"),
    ({"operation": "starting", "known": False}, "starting", "start-pending"),
    ({"operation": "stopping", "known": False}, "stopping", "stop-pending"),
    ({"known": False}, "unknown", "source-unavailable"),
    ({"contact_current": False, "open_count": 1}, "unknown", "carrier-stale"),
    ({"conflict": True}, "unknown", "conflicting-facts"),
    ({"scope": "carrier-deliveries"}, "unknown", "partial-turn-coverage"),
    ({"provider_block": True}, "blocked", "provider-block"),
    ({"permission_supported": True, "waits": (("wait", 1),), "open_count": 1}, "blocked", "permission-wait"),
])
def test_precedence(changes, state, reason):
    row = derive(**changes)
    assert (row["state"], row["reason"]) == (state, reason)
    assert row["coverage"]["permissionWait"] is changes.get("permission_supported", False)


def test_threshold_boundary_and_activity_do_not_clear_run_age():
    facts = RunFacts(source_epoch="e", known=True, running=True, open_count=1,
                     since_ms=100, since_monotonic_ms=100, activity_supported=True,
                     quiet=True, last_activity_at_ms=90)
    policy = RunPolicy(turn_long_after_ms=100)
    before = derive_run_state(facts, policy, now_ms=199, monotonic_ms=199)
    assert before["suspicions"] == ["producer-quiet"]
    at = derive_run_state(facts, policy, now_ms=200, monotonic_ms=200)
    assert at["suspicions"] == ["turn-over-budget", "producer-quiet"]
    resumed = derive_run_state(replace(facts, quiet=False), policy, now_ms=201, monotonic_ms=201)
    assert resumed["suspicions"] == ["turn-over-budget"]
    ended = derive_run_state(replace(facts, open_count=0), policy, now_ms=201, monotonic_ms=201)
    assert ended["state"] == "idle" and ended["turnAgeMs"] is None


def test_permission_wait_long_stays_blocked_and_missing_capability_is_not_a_wait():
    facts = RunFacts(source_epoch="e", known=True, running=True, open_count=1,
                     permission_supported=True, waits=(("a", 0), ("b", 100)))
    row = derive_run_state(facts, RunPolicy(permission_long_after_ms=200), now_ms=200, monotonic_ms=200)
    assert row["state"] == "blocked" and row["baseState"] == "in-turn"
    assert row["suspicions"] == ["permission-wait-long"]
    assert derive_run_state(replace(facts, waits=(("b", 100),)), RunPolicy(permission_long_after_ms=200),
                            now_ms=200, monotonic_ms=200)["suspicions"] == []
    assert derive_run_state(replace(facts, permission_supported=False), RunPolicy(),
                            now_ms=200, monotonic_ms=200)["state"] == "in-turn"


def test_unknown_duration_never_uses_wall_clock_or_route_c_silence():
    row = derive(open_count=1, since_ms=1, since_monotonic_ms=None)
    assert row["turnAgeMs"] is None and row["suspicions"] == []
    assert row["coverage"]["producerActivity"] is False
    assert derive(exit_verified=True, provider_block=True)["providerBlocked"] is True


@pytest.mark.parametrize("value", [-1, True, "1", 0])
def test_policy_rejects_invalid_thresholds(value):
    with pytest.raises(ValueError):
        RunPolicy(turn_long_after_ms=value)


def test_policy_defaults_overrides_and_disable():
    policies = RunPolicies.from_payload({"defaults": {"turnLongAfterMs": 100},
                                       "agents": {"agent:o:n:a": {"turnLongAfterMs": None}}})
    assert policies.for_actor("agent:o:n:b").turn_long_after_ms == 100
    assert policies.for_actor("agent:o:n:a").turn_long_after_ms is None
    with pytest.raises(ValueError):
        RunPolicies.from_payload({"defaults": {"autoKill": True}})
