import threading

import pytest

from hyprial.kernel import ActorSpec, GenerationScheduler, create_actor_host
from hyprial.biz.impl.usage import SourceSnapshot
from hyprial.biz.impl.usage_actor import UsageAuthority


@pytest.fixture
def dependencies():
    host = create_actor_host()
    scheduler = GenerationScheduler()
    yield host, scheduler
    scheduler.shutdown()
    assert host.shutdown()


def test_unbound_usage_fails_closed(tmp_path):
    actor = UsageAuthority(home=tmp_path)
    try:
        with pytest.raises(RuntimeError, match="bound"):
            actor.start()
        with pytest.raises(RuntimeError, match="bound"):
            actor.refresh_once()
    finally:
        assert actor.stop()


def test_binding_is_one_shot_and_stop_borrows_dependencies(tmp_path, dependencies):
    host, scheduler = dependencies
    actor = UsageAuthority(home=tmp_path)
    actor.bind(host=host, scheduler=scheduler)
    actor.bind(host=host, scheduler=scheduler)
    with pytest.raises(ValueError, match="bound"):
        actor.bind(host=object(), scheduler=scheduler)
    assert actor.stop()
    fired = threading.Event()
    scheduler.schedule("daemon-maintenance", 1, 0, lambda generation: fired.set())
    assert fired.wait(1)
    cell = host.spawn(ActorSpec(name="still-live", handler_factory=lambda: lambda _: None))
    assert cell.stop()


def test_blocked_collector_does_not_block_projection_or_shutdown(tmp_path, monkeypatch, dependencies):
    entered, release = threading.Event(), threading.Event()
    actor = UsageAuthority(home=tmp_path)
    actor.bind(host=dependencies[0], scheduler=dependencies[1])

    def collect():
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(actor._collector, "refresh_once", collect)
    try:
        actor.start()
        assert entered.wait(1)
        assert actor.snapshots() == ()
        assert len(actor.snapshot_payload()["sources"]) == 3
        assert not actor.stop(timeout=0.01)
    finally:
        release.set()
        assert actor.stop(timeout=2)
    assert actor.snapshots() == ()  # late completion cannot publish after close


def test_observer_sees_published_snapshot_and_cannot_mutate_cached_extras(
    tmp_path, monkeypatch, dependencies
):
    notified = threading.Event()
    actor = UsageAuthority(home=tmp_path, actor_host=dependencies[0], scheduler=dependencies[1])
    monkeypatch.setattr(actor._collector, "refresh_once", lambda: None)
    monkeypatch.setattr(
        actor._collector,
        "snapshots",
        lambda: (SourceSnapshot("claude", True, 1, extra={"nested": [1]}),),
    )
    actor.set_refresh_observer(notified.set)
    try:
        actor.start()
        assert notified.wait(1)
        first = actor.snapshots()
        first[0].extra["nested"].append(2)
        assert actor.snapshots()[0].extra == {"nested": [1]}
    finally:
        assert actor.stop()
