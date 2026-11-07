import threading

from hyprial.biz.impl.usage import SourceSnapshot
from hyprial.biz.impl.usage_actor import UsageAuthority


def test_blocked_collector_does_not_block_projection_or_shutdown(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    actor = UsageAuthority(home=tmp_path)

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
    tmp_path, monkeypatch
):
    notified = threading.Event()
    actor = UsageAuthority(home=tmp_path)
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
