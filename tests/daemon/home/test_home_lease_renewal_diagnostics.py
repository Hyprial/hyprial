"""A fenced renewal records which stage still owns work, without relaxing TTL."""

import threading

from hyprial.daemon.impl.configuration.home_guard import ActiveDaemonHeartbeat
from hyprial.daemon.impl.configuration.home_lease_actor import HomeLeaseAuthority


def test_stalled_file_renewal_reports_io_stage_at_original_fence(tmp_path, monkeypatch):
    entered, release, lost = threading.Event(), threading.Event(), threading.Event()
    original = ActiveDaemonHeartbeat._renew_record

    def held(self):
        entered.set()
        assert release.wait(3)
        return original(self)

    monkeypatch.setattr(ActiveDaemonHeartbeat, "_renew_record", held)
    owner = HomeLeaseAuthority(
        tmp_path, keepalive_duration=0.03,
        self_fence_on_expiry=True,
        identity_reader=lambda _pid: "synthetic-identity", ownership_lost=lost.set,
    )
    try:
        owner.claim()
        assert entered.wait(1)
        assert lost.wait(1)
        status = owner.status()
        assert status["fenceReason"] == "heartbeat-deadline"
        assert status["keepaliveSeconds"] == 0.03
        timing = status["renewalTiming"]
        assert timing["scheduledFor"] <= timing["queuedAt"]
        assert timing["queuedAt"] <= timing["actorReceivedAt"] <= timing["effectSubmittedAt"]
        assert timing["effectSubmittedAt"] <= timing["ioStartedAt"] <= timing["fencedAt"]
        assert timing["ioCompletedAt"] is None
        assert timing["completionReceivedAt"] is None
        assert not owner.close(0.01)
    finally:
        release.set()
        assert owner.close(2)
    settled = owner.status()["renewalTiming"]
    assert settled["ioCompletedAt"] >= timing["fencedAt"]
    assert settled["completionReceivedAt"] >= settled["ioCompletedAt"]
    # A previously returned projection is immutable to later worker updates.
    assert timing["ioCompletedAt"] is None
