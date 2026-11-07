import json
import threading
import time

import pytest

from hyprial.kernel.impl.actor_runtime import ActorState
from hyprial.kernel.impl.actor_runtime.scheduler import GenerationScheduler
from hyprial.daemon.impl.configuration import home_lease_actor
from hyprial.daemon.impl.configuration.home_lease_actor import HomeLeaseAuthority, LeaseExpired
from hyprial.daemon.impl.configuration.home_guard import ActiveDaemonHeartbeat, HYPRIALHomeInUse
from hyprial.daemon.impl.mcp.channel.ownership import _OwnerProcessStatus


class _ManualTimer:
    def __init__(self, delay, callback, args):
        self.callback, self.args = callback, args
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        pass

    def fire(self):
        # A cancelled callback may already have been queued.
        self.callback(*self.args)


def _manual_scheduler(monkeypatch):
    monkeypatch.setattr(
        home_lease_actor, "GenerationScheduler",
        lambda: GenerationScheduler(timer_factory=_ManualTimer),
    )


def _wait_for(predicate):
    deadline = time.monotonic() + 1
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


@pytest.mark.parametrize("field", ["generation", "pid", "processIdentity"])
def test_release_never_removes_a_replacement_generation(tmp_path, field):
    lost = threading.Event()
    actor = HomeLeaseAuthority(
        tmp_path,
        keepalive_duration=0.03,
        identity_reader=lambda pid: "synthetic-identity",
        ownership_lost=lost.set,
    )
    actor.claim()
    replacement = json.loads(actor.path.read_text())
    replacement[field] = actor._core.pid + 1 if field == "pid" else "replacement"
    raw = json.dumps(replacement)
    actor.path.write_text(raw)
    try:
        assert lost.wait(1)
    finally:
        assert actor.close()
    assert actor.path.read_text() == raw


def test_lease_claim_renew_release_keep_physical_generation_fence(tmp_path):
    actor = HomeLeaseAuthority(
        tmp_path,
        keepalive_duration=0.03,
        identity_reader=lambda pid: "synthetic-identity",
        duplicate_observe_window=0.05,
    )
    try:
        actor.claim()
        original = json.loads(actor.path.read_text())
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            renewed = json.loads(actor.path.read_text())
            if renewed["heartbeatMonotonic"] > original["heartbeatMonotonic"]:
                break
            time.sleep(0.005)
        assert renewed["generation"] == actor.generation
        assert renewed["heartbeatMonotonic"] > original["heartbeatMonotonic"]
    finally:
        assert actor.close()
    assert not actor.path.exists()


def test_stalled_renewal_fences_lease_at_existing_stale_threshold(
    tmp_path, monkeypatch
):
    entered, release, lost = threading.Event(), threading.Event(), threading.Event()
    original = ActiveDaemonHeartbeat._renew_record

    def stalled(self):
        entered.set()
        assert release.wait(3)
        return original(self)

    monkeypatch.setattr(ActiveDaemonHeartbeat, "_renew_record", stalled)
    actor = HomeLeaseAuthority(
        tmp_path,
        self_fence_on_expiry=True,
        keepalive_duration=0.03,
        identity_reader=lambda pid: "synthetic-identity",
        ownership_lost=lost.set,
    )
    try:
        actor.claim()
        assert entered.wait(1)
        assert lost.wait(1)
        assert not actor.close(0.01)
    finally:
        release.set()
        assert actor.close(2)
    assert not actor.path.exists()


@pytest.mark.parametrize("first_timer", ["expiry", "renew"])
def test_one_second_owner_paused_four_seconds_has_one_live_lease(
    tmp_path, monkeypatch, first_timer,
):
    """Both due-timer orders retain one owner; only committed renew rescues it."""
    _manual_scheduler(monkeypatch)
    now = [100.0]
    lost = threading.Event()
    options = dict(
        monotonic_clock=lambda: now[0],
        identity_reader=lambda pid: f"identity-{pid}",
        process_status=lambda _pid, _identity: _OwnerProcessStatus.ALIVE,
        claim_wait_timeout=0,
    )
    owner = HomeLeaseAuthority(tmp_path, pid=111, keepalive_duration=1,
                               self_fence_on_expiry=True,
                               ownership_lost=lost.set, **options)
    successor = HomeLeaseAuthority(tmp_path, pid=222, keepalive_duration=5, **options)

    def safety_cut(expected_pid):
        states = [lease.status() for lease in (owner, successor)]
        live = [state["claimed"] and not state["closing"] for state in states]
        assert sum(live) <= 1, states
        if expected_pid is not None:
            assert json.loads(owner.path.read_text())["pid"] == expected_pid

    try:
        owner.claim()
        safety_cut(111)
        renew = owner._scheduler._entries["renew"][1]
        expiry = owner._scheduler._entries["expiry"][1]
        # No scheduled callback can run during the four-second pause.
        now[0] += 4.0
        safety_cut(111)
        if first_timer == "expiry":
            expiry.fire()
            assert lost.wait(1)
            assert owner.status()["fenceReason"] == "heartbeat-deadline"
            safety_cut(111)
            assert owner.close(2)
            assert owner._runtime.snapshot(owner._handle).state is ActorState.STOPPED
            successor.claim()
            safety_cut(222)
            renew.fire()
            safety_cut(222)
            assert not owner.status()["claimed"]
        else:
            renew.fire()
            deadline = time.monotonic() + 1
            while owner.status()["version"] == 1:
                assert time.monotonic() < deadline
                time.sleep(0.005)
            assert json.loads(owner.path.read_text())["heartbeatMonotonic"] == now[0]
            safety_cut(111)
            expiry.fire()
            assert not lost.is_set()
            with pytest.raises(HYPRIALHomeInUse):
                successor.claim()
            safety_cut(111)
            assert owner.status()["claimed"]
    finally:
        assert successor.close(2)
        assert owner.close(2)


@pytest.mark.parametrize("late_claim_receipt", [False, True])
def test_default_expired_owner_renews_without_ttl_exit(
    tmp_path, monkeypatch, late_claim_receipt,
):
    _manual_scheduler(monkeypatch)
    now = [100.0]
    lost = threading.Event()
    owner = HomeLeaseAuthority(
        tmp_path, keepalive_duration=1,
        monotonic_clock=lambda: now[0],
        identity_reader=lambda _pid: "identity",
        ownership_lost=lost.set,
    )
    claim = owner._core._claim_record

    def delayed_claim_receipt():
        result = claim()
        now[0] += 4
        return result

    if late_claim_receipt:
        monkeypatch.setattr(owner._core, "_claim_record", delayed_claim_receipt)
    try:
        owner.claim()
        assert "expiry" not in owner._scheduler._entries
        assert owner.status()["claimed"] and not owner.status()["closing"]
        if not late_claim_receipt:
            now[0] += 4
        version = owner.status()["version"]
        # Default-off also ignores a stale expiry already delivered to the
        # actor, not merely timers it elects not to schedule.
        owner._runtime.tell(owner._handle, LeaseExpired(owner.generation, version))
        owner._scheduler._entries["renew"][1].fire()
        _wait_for(lambda: lost.is_set() or owner.status()["version"] > version)
        assert not lost.is_set()
        assert owner.status()["claimed"] and not owner.status()["closing"]
        assert owner.status()["fenceReason"] is None
        assert json.loads(owner.path.read_text())["heartbeatMonotonic"] == now[0]
        assert "expiry" not in owner._scheduler._entries
    finally:
        assert owner.close(2)


def test_default_renewal_write_failure_still_loses_ownership(tmp_path, monkeypatch):
    _manual_scheduler(monkeypatch)
    lost = threading.Event()
    owner = HomeLeaseAuthority(
        tmp_path, keepalive_duration=1,
        identity_reader=lambda _pid: "identity", ownership_lost=lost.set,
    )
    try:
        owner.claim()

        def failed_write():
            raise OSError("renewal write refused")

        monkeypatch.setattr(owner._core, "_write_record", failed_write)
        owner._scheduler._entries["renew"][1].fire()
        assert lost.wait(1)
        assert owner.status()["closing"] and not owner.status()["claimed"]
    finally:
        assert owner.close(2)


def test_expiry_policy_rejects_a_non_boolean_before_starting(tmp_path):
    with pytest.raises(TypeError, match="self_fence_on_expiry must be a bool"):
        HomeLeaseAuthority(tmp_path, self_fence_on_expiry="false")


def test_opt_in_late_claim_still_fences_at_original_deadline(tmp_path, monkeypatch):
    _manual_scheduler(monkeypatch)
    now = [100.0]
    lost = threading.Event()
    owner = HomeLeaseAuthority(
        tmp_path, keepalive_duration=1, self_fence_on_expiry=True,
        monotonic_clock=lambda: now[0],
        identity_reader=lambda _pid: "identity", ownership_lost=lost.set,
    )
    claim = owner._core._claim_record

    def delayed_claim_receipt():
        result = claim()
        now[0] += 4
        return result

    monkeypatch.setattr(owner._core, "_claim_record", delayed_claim_receipt)
    try:
        with pytest.raises(TimeoutError, match="expired before claim settled"):
            owner.claim()
        assert lost.is_set()
        assert owner.status()["fenceReason"] == "heartbeat-already-expired"
        assert not owner.status()["claimed"]
    finally:
        assert owner.close(2)
