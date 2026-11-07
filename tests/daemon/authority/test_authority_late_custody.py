"""Late reply retirement must not revoke a joiner's accepted completion."""
from collections import OrderedDict
from types import SimpleNamespace
import threading

import pytest

from hyprial.daemon.impl import state_persistence as state
from hyprial.daemon.impl.pac.graphs import authority as pac


@pytest.mark.parametrize("module,authority_class", [
    (state, state.StatePersistenceAuthority),
    (pac, pac.PacGraphAuthority),
])
def test_live_joiner_keeps_completion_after_late_cache_eviction(module, authority_class):
    # Exercise the real completion/retirement methods with a deterministic
    # waiter scheduling seam; no database or native side effect is mocked.
    authority = authority_class.__new__(authority_class)
    authority._lock = threading.Lock()
    authority._pending = {}
    authority._late_results = OrderedDict()
    authority._completion_capacity = 1
    pending = module._Pending()
    pending.abandoned = True
    authority._pending["first"] = pending

    def complete(key, value):
        if module is state:
            authority._complete(key, 1, value, None)
        else:
            authority._complete(pac.GraphCommandCompleted(key, 1, value))

    class ScheduledEvent:
        def set(self):
            pass

        def wait(self, timeout):
            # The original mutation settles, then an unrelated completion
            # evicts its cache entry before the blocked caller gets CPU again.
            complete("first", {"receipt": "first"})
            other = module._Pending()
            other.abandoned = True
            authority._pending["second"] = other
            complete("second", {"receipt": "second"})
            assert "first" not in authority._late_results
            return True

    pending.event = ScheduledEvent()
    result = authority.settled_result("first")
    assert result is not None
    assert result.result == {"receipt": "first"}
    assert len(authority._late_results) == 1
    assert not authority._pending


@pytest.mark.parametrize("module,authority_class,error_class", [
    (state, state.StatePersistenceAuthority, state.StatePersistenceBusy),
    (pac, pac.PacGraphAuthority, pac.PacGraphOverloaded),
])
def test_never_begun_write_gets_explicit_rejection(module, authority_class, error_class):
    authority = authority_class.__new__(authority_class)
    authority._lock = threading.Lock()
    authority._pending = {"queued": module._Pending()}
    authority._late_results = OrderedDict()
    authority._completion_capacity = 1
    authority._generation = 4
    authority._undelivered(SimpleNamespace(correlation_id="queued"), "ACTOR_STOPPED_UNDELIVERED")
    result = authority.settled_result("queued", timeout=0)
    assert isinstance(result.error, error_class)
    assert str(result.error) == "ACTOR_STOPPED_UNDELIVERED"
    assert not authority._pending
