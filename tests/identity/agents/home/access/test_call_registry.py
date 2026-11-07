"""Home-operation correlation retains pending custody and bounded results."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import inspect
import json
from pathlib import Path

import pytest

from hyprial.identity.impl.agents.actor._home_calls import (
    HomeCallRegistry,
    HomeOperationConflict,
    HomeOperationUnknown,
)
from hyprial.identity.impl.agents.actor._base import AgentHomeAccessError


def test_default_memory_bounds_match_declared_supervision_contract():
    manifest = Path(__file__).resolve().parents[5] / "contract/identity/home-access-bounds.json"
    declared = json.loads(manifest.read_text())
    parameters = inspect.signature(HomeCallRegistry).parameters
    for key in ("pending_capacity", "settled_capacity", "retention_seconds"):
        assert parameters[key].default == declared[key]


def test_concurrent_same_operation_reserves_once_and_shares_one_waiter():
    calls = HomeCallRegistry()
    barrier = Barrier(8)

    def reserve(_index):
        barrier.wait(timeout=3)
        return calls.reserve("op:land:0", "fingerprint")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    assert sum(new for _call, new in results) == 1
    assert all(call is results[0][0] for call, _new in results)
    calls.settle("op:land:0", "home", None)
    assert all(call.event.is_set() and call.value == "home" for call, _ in results)


@pytest.mark.parametrize("settled", [False, True])
def test_different_fingerprint_never_overwrites_original(settled):
    calls = HomeCallRegistry()
    original, _new = calls.reserve("op", "original-token-and-payload")
    if settled:
        calls.settle("op", "original-result", None)
    with pytest.raises(HomeOperationConflict):
        calls.reserve("op", "different-token-or-payload")
    assert calls.lookup("op") is original
    if settled:
        assert original.value == "original-result"


def test_pending_does_not_expire_or_lose_custody_under_cache_pressure():
    now = [0.0]
    calls = HomeCallRegistry(clock=lambda: now[0], pending_capacity=1, settled_capacity=1, retention_seconds=2)
    pending, _ = calls.reserve("pending", "a")
    now[0] = 10_000.0
    assert calls.lookup("pending") is pending
    assert calls.reserve("pending", "a") == (pending, False)
    with pytest.raises(AgentHomeAccessError, match="capacity"):
        calls.reserve("other", "b")
    assert calls.lookup("pending") is pending and not pending.event.is_set()
    calls.settle("pending", "done", None)
    _other, new = calls.reserve("other", "b")
    assert new


def test_completed_result_is_shared_until_age_boundary_without_renewal():
    now = [0.0]
    calls = HomeCallRegistry(clock=lambda: now[0], retention_seconds=5)
    call, _ = calls.reserve("op", "a")
    now[0] = 100.0  # Retention starts at completion, not submission.
    calls.settle("op", "home", None)
    now[0] = 104.0
    assert calls.reserve("op", "a") == (call, False)
    assert calls.lookup("op").value == "home"
    now[0] = 105.0
    with pytest.raises(HomeOperationUnknown):
        calls.lookup("op")


def test_capacity_evicts_completed_only_and_does_not_invalidate_attached_waiter():
    calls = HomeCallRegistry(settled_capacity=1)
    first, _ = calls.reserve("first", "a")
    calls.settle("first", "one", None)
    pending, _ = calls.reserve("pending", "p")
    calls.reserve("second", "b")
    calls.settle("second", "two", None)
    with pytest.raises(HomeOperationUnknown):
        calls.lookup("first")
    assert first.event.is_set() and first.value == "one"
    assert calls.lookup("second").value == "two"
    assert calls.lookup("pending") is pending


def test_unknown_lookup_is_read_only_even_after_restart():
    original = HomeCallRegistry()
    original.reserve("old", "a")
    original.settle("old", "home", None)
    restarted = HomeCallRegistry(pending_capacity=1)
    for operation in ("old", "never-seen", "old"):
        with pytest.raises(HomeOperationUnknown):
            restarted.lookup(operation)
    _call, new = restarted.reserve("genuinely-new", "b")
    assert new, "unknown lookups must not consume submission capacity"


def test_first_completion_wins_including_failure():
    calls = HomeCallRegistry()
    call, _ = calls.reserve("op", "a")
    failure = RuntimeError("filesystem failed")
    calls.settle("op", None, failure)
    calls.settle("op", "late-success", None)
    assert calls.lookup("op") is call
    assert call.event.is_set() and call.error is failure and call.value is None


@pytest.mark.parametrize("field,value", [("pending_capacity", 0), ("settled_capacity", 0), ("retention_seconds", 0), ("retention_seconds", float("nan")), ("retention_seconds", float("inf"))])
def test_cache_bounds_must_be_positive_and_finite(field, value):
    with pytest.raises(ValueError):
        HomeCallRegistry(**{field: value})
