"""Read-only lifecycle observation: cursors are diagnostics, never custody."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from hyprial.kernel import LifecycleTarget
from hyprial.daemon.impl.lifecycle.observation.projection import LifecycleObservationProjection


TARGET = LifecycleTarget("host", "harness.lifecycle", "agent:owner:host:worker/incarnation")


def publish(owner, state="running", epoch="binding-1"):
    return owner.publish(TARGET, epoch=epoch, state=state, cause="owner-fact", available=True)


def test_first_observation_absent_and_already_stopped():
    owner = LifecycleObservationProjection()
    missing = owner.observe(TARGET)
    assert missing.current.cause == "noproc"
    assert not missing.current.available
    publish(owner, "stopped")
    stopped = owner.observe(TARGET)
    assert stopped.current.state == "stopped"
    assert stopped.cursor.revision == stopped.current.revision


def test_exit_between_snapshot_and_observe_cannot_disappear():
    owner = LifecycleObservationProjection()
    before = publish(owner)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(publish, owner, "stopped").result()
    after = owner.observe(TARGET, epoch=before.epoch, after_revision=before.revision)
    assert [event.transition for event in after.events] == ["stopped"]
    assert after.current.state == "stopped"


def test_retention_epoch_and_invalid_cursor_require_resync():
    owner = LifecycleObservationProjection(history_limit=2)
    before = publish(owner)
    for state in ("stopping", "stopped", "running"):
        publish(owner, state)
    lost = owner.observe(TARGET, epoch=before.epoch, after_revision=before.revision)
    assert lost.resync_required and lost.current.state == "running"
    latest = publish(owner, epoch="binding-2")
    assert owner.observe(TARGET, epoch=before.epoch, after_revision=4).resync_required
    for revision in (-1, 2, 999):
        assert owner.observe(TARGET, epoch=latest.epoch, after_revision=revision).resync_required


def test_pagination_cursor_does_not_skip_events_for_current_snapshot():
    owner = LifecycleObservationProjection()
    before = publish(owner)
    publish(owner, "stopping")
    publish(owner, "stopped")
    page = owner.observe(TARGET, epoch=before.epoch, after_revision=before.revision, limit=1)
    assert page.current.revision == 3
    assert page.cursor.revision == 2
    rest = owner.observe(TARGET, epoch=page.cursor.epoch, after_revision=page.cursor.revision)
    assert [event.transition for event in rest.events] == ["stopped"]


def test_observer_overload_cancel_and_shutdown_do_not_stop_target():
    owner = LifecycleObservationProjection(observer_limit=2)
    publish(owner)
    first, second = owner.open(TARGET), owner.open(TARGET)
    assert first.status == second.status == "ok"
    assert owner.open(TARGET).status == "overloaded"
    owner.cancel(first.observer_id)
    assert owner.observe(TARGET, observer_id=first.observer_id).status == "closed"
    assert owner.open(TARGET).status == "ok"
    assert publish(owner).state == "running"
    owner.close()
    assert owner.observe(TARGET, observer_id=second.observer_id).status == "closed"
    assert owner.open(TARGET).status == "closed"
    # No process handle, lifecycle command, or target cancellation crosses this API.


def test_unknown_source_and_capacity_eviction_are_not_stopped():
    owner = LifecycleObservationProjection(target_limit=1)
    before = publish(owner)
    other = LifecycleTarget("host", "harness.lifecycle", "other/incarnation")
    owner.publish(other, epoch="other", state="unknown", cause="unavailable", available=False)
    batch = owner.observe(TARGET, epoch=before.epoch, after_revision=before.revision)
    assert batch.resync_required
    assert not batch.current.available
    assert batch.current.state == "unknown"


def test_evicted_target_republished_under_same_source_epoch_requires_resync():
    owner = LifecycleObservationProjection(target_limit=1)
    before = publish(owner)
    other = LifecycleTarget("host", "harness.lifecycle", "other/incarnation")
    owner.publish(other, epoch="other", state="running", cause="owner-fact", available=True)
    publish(owner)
    assert owner.observe(TARGET, epoch=before.epoch, after_revision=before.revision).resync_required


def test_absent_cursor_cannot_hide_a_create_exit_and_forget():
    owner = LifecycleObservationProjection()
    absent = owner.snapshot(TARGET)
    publish(owner)
    publish(owner, "stopped")
    owner.forget(TARGET)
    batch = owner.observe(TARGET, epoch=absent.epoch, after_revision=absent.revision)
    assert batch.resync_required


def test_namespace_wait_and_limit_are_explicit_not_silently_ignored():
    owner = LifecycleObservationProjection()
    with pytest.raises(ValueError):
        owner.observe(TARGET, namespace="inbox")
    with pytest.raises(ValueError):
        owner.observe(TARGET, wait_ms=1)
    with pytest.raises(ValueError):
        owner.observe(TARGET, limit=0)
