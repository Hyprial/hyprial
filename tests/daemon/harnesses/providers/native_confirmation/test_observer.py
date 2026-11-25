"""Only an admitted successful response on the bound launch confirms native Auth."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.api import HarnessDelivery, HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.harnesses.runtime.native_auth import native_auth_observer


@pytest.mark.parametrize("change", [
    {}, {"status": HarnessResultStatus.FAILED}, {"status": HarnessResultStatus.INTERRUPTED},
    {"output": ""}, {"output": "  "}, {"error": "failed"}, {"failure_code": "failure"},
    {"delivery_id": "other"}, {"recipient": "other"},
])
def test_completion_is_per_delivery_and_does_not_replace_hook_observer(change):
    confirmations, hooks, starts = [], [], []
    def hook(*args, **kwargs):
        hooks.append(args)
    hook.turn_started = lambda *args, **kwargs: starts.append(args)
    launch = SimpleNamespace(native_auth_launch=SimpleNamespace(confirm=confirmations.append))
    observer = native_auth_observer(launch, hook)
    delivery = HarnessDelivery("delivery", "conversation", "sender", "recipient", "request")
    result = replace(HarnessResult("delivery", "recipient", HarnessResultStatus.COMPLETED, output="response"), **change)
    observer.turn_started(delivery, started_at_ms=1000)
    observer(delivery, result, started_at_ms=1000, ended_at_ms=2000, tool_names=())
    assert confirmations == ([] if change else [2000])
    assert len(hooks) == len(starts) == 1


def test_absent_proof_preserves_observer_and_absent_hook_still_records():
    hook = object()
    assert native_auth_observer(None, hook) is hook
    times = []
    observer = native_auth_observer(SimpleNamespace(native_auth_launch=SimpleNamespace(confirm=times.append)), None)
    delivery = HarnessDelivery("delivery", "conversation", "sender", "recipient", "request")
    result = HarnessResult("delivery", "recipient", HarnessResultStatus.COMPLETED, output="response")
    observer.turn_started(delivery, started_at_ms=1000)
    observer(delivery, result, started_at_ms=1000, ended_at_ms=2000, tool_names=())
    assert times == [2000]
