"""Automatic restart consumes only the current in-memory custody observation."""
import pytest
from ..source import test_start_hold as fixtures
from ..source.hold_support import spec

harness_actor = fixtures.harness_actor


@pytest.mark.parametrize("mode", ["held", "unavailable", "raises"])
def test_reconcile_cannot_fail_open_when_custody_is_not_clear(harness_actor, mode):
    runtime, launcher, _, events = harness_actor()
    assert runtime.start(spec())
    calls = []
    def guard(actor, token):
        calls.append((actor, token))
        if mode == "raises":
            raise RuntimeError("unavailable")
        return False
    runtime._actor._source_custody_clear = guard
    # Use an already-observed stopped process; ProcessOwner.refresh itself is
    # asynchronous and is not the custody behavior under test.
    runtime._actor._records["fake:source"].process = launcher.processes[0]
    launcher.processes[0].running = False
    runtime.reconcile()
    events.barrier(runtime._actor)
    assert calls == [("source", "source-incarnation")], "automatic start bypassed custody projection"
    assert len(launcher.processes) == 1
    assert runtime._actor._automatic_custody_clear("source") is False


def test_reconcile_passes_current_incarnation_not_retired_token(harness_actor):
    runtime, launcher, identity, events = harness_actor()
    assert runtime.start(spec())
    identity.token = "replacement"
    seen = []
    runtime._actor._source_custody_clear = lambda actor, token: seen.append((actor, token)) or False
    # Use an already-observed stopped process; ProcessOwner.refresh itself is
    # asynchronous and is not the custody behavior under test.
    runtime._actor._records["fake:source"].process = launcher.processes[0]
    launcher.processes[0].running = False
    runtime.reconcile()
    events.barrier(runtime._actor)
    assert seen == [("source", "replacement")]
    assert len(launcher.processes) == 1
