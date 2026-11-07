"""A fresh heartbeat cannot repair lost turn-state coverage."""
from hyprial.daemon.impl.session_actor.internals import _SessionRuntimeState


def test_gap_is_detected_even_without_a_status_read_during_disconnect():
    owner = _SessionRuntimeState("daemon", lease_ttl_seconds=1)
    owner.register("actor", "session", 0, confirmed=True)
    owner.register("actor", "session", 500, confirmed=True)
    assert not owner.read("actor", "session", now_ms=500, ttl_seconds=1).observation_gap
    owner.register("actor", "session", 2000, confirmed=True)
    current = owner.read("actor", "session", now_ms=2000, ttl_seconds=1)
    assert current.alive and current.observation_gap
    owner.register("actor", "session", 2200, confirmed=True)
    assert owner.read("actor", "session", now_ms=2200, ttl_seconds=1).observation_gap
    owner.register("actor", "replacement", 2300, confirmed=True)
    assert not owner.read("actor", "replacement", now_ms=2300, ttl_seconds=1).observation_gap
    owner.lifecycle_observations.close()
