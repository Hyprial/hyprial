"""A restore failure is reported only after its desired-state write settles."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hyprial.kernel.impl.actor_runtime import ActorState, AdmissionResult
from hyprial.kernel.impl.contracts.ports import PortCommandRejected
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.desired_state_io import DesiredStateIoPort, DesiredStateOperation
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.actor.facade import HarnessRuntimeFacade
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessRestoreCompleted

from tests.daemon.lifecycle.lifecycle.ports.support import _Events, _Launcher


def _runtime(tmp_path: Path):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.upsert_harness(HarnessLaunchSpec("codex", "veteran", True))
    events = _Events()
    actor = HarnessRuntimeActor(
        _Launcher(start_failures=1), event_sink=events,
        desired_state=store, failure_budget=3,
    )
    return store, events, actor, HarnessRuntimeFacade(actor)


def _restore(facade: HarnessRuntimeFacade, store: DesiredStateStore):
    return facade.restore(tuple(store.load().harnesses))


@pytest.mark.parametrize("restart_guardian", [False, True])
def test_restore_failure_waits_for_exact_committed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restart_guardian: bool,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    original = DesiredStateIoPort._execute_owned

    def held_write(self, request):
        if request.operation is DesiredStateOperation.MARK_HARNESS_FAILED:
            entered.set()
            assert release.wait(5.0)
        return original(self, request)

    monkeypatch.setattr(DesiredStateIoPort, "_execute_owned", held_write)
    store, events, actor, facade = _runtime(tmp_path)
    result = []
    errors = []
    done = threading.Event()

    def restore():
        try:
            result.append(_restore(facade, store))
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    waiter = threading.Thread(target=restore, daemon=True)
    waiter.start()
    try:
        assert entered.wait(2.0)
        # This fails on the prefixed actor tree: the restore call returned
        # failed=1 while the durable row was still running behind this gate.
        assert not done.wait(0.1)
        assert store.load().harnesses[0].status == "running"
        assert facade.drain_readiness_reports() == ()
        assert facade.drain_failed_events() == ()
        if restart_guardian:
            first_generation = actor._handler_generation
            crash_once = True

            def fail_once(_event):
                nonlocal crash_once
                if crash_once:
                    crash_once = False
                    raise RuntimeError("synthetic guardian restart")

            actor.subscribe_events(fail_once)
            assert actor._admit(object()) is AdmissionResult.ACCEPTED
            deadline = time.monotonic() + 2
            while actor._handler_generation == first_generation:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            while actor._runtime.snapshot(actor._handle).state is not ActorState.RUNNING:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert not done.is_set()
        release.set()
        waiter.join(3.0)
        assert done.is_set() and errors == []
        assert len(result) == 1 and result[0].failed == 1
        assert store.load().harnesses[0].status == "failed"
        assert [report.verdict for report in facade.drain_readiness_reports()] == ["failed"]
        assert facade.drain_failed_events() == ("codex:veteran",)
        with events._condition:
            terminal = [event for event in events._events if isinstance(event, HarnessRestoreCompleted)]
        assert len(terminal) == 1
    finally:
        release.set()
        waiter.join(3.0)
        assert actor.close_runtime(3.0)


@pytest.mark.parametrize("failure", ["refused", "write_error"])
def test_restore_persistence_failure_is_one_correlated_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    store, events, actor, facade = _runtime(tmp_path)
    if failure == "refused":
        assert actor._persistence is not None
        monkeypatch.setattr(
            actor._persistence, "submit", lambda _request: AdmissionResult.OVERLOADED
        )
        expected_code = "HARNESS_PERSISTENCE_OVERLOADED"
    else:
        def fail_write(_harness, _name):
            raise OSError("synthetic desired-state write failure")

        monkeypatch.setattr(store, "mark_harness_failed", fail_write)
        expected_code = "HARNESS_PERSISTENCE_FAILED"
    try:
        with pytest.raises(RuntimeError) as caught:
            _restore(facade, store)
        assert caught.value.code == expected_code
        assert store.load().harnesses[0].status == "running"
        assert facade.drain_readiness_reports() == ()
        assert facade.drain_failed_events() == ()
        with events._condition:
            terminal = [
                event for event in events._events
                if isinstance(event, (HarnessRestoreCompleted, PortCommandRejected))
            ]
        assert len(terminal) == 1
        assert isinstance(terminal[0], PortCommandRejected)
        assert terminal[0].code == expected_code
        assert terminal[0].correlation_id
    finally:
        assert actor.close_runtime(3.0)


def test_failed_write_does_not_abandon_other_restore_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DesiredStateStore(tmp_path / "desired-state.json")
    for name in ("alpha", "beta"):
        store.upsert_harness(HarnessLaunchSpec("codex", name, True))
    original = store.mark_harness_failed

    def fail_alpha(harness, name):
        if name == "alpha":
            raise OSError("synthetic alpha write failure")
        return original(harness, name)

    monkeypatch.setattr(store, "mark_harness_failed", fail_alpha)
    events = _Events()
    actor = HarnessRuntimeActor(
        _Launcher(start_failures=10), event_sink=events,
        desired_state=store, failure_budget=3,
    )
    facade = HarnessRuntimeFacade(actor)
    try:
        with pytest.raises(RuntimeError) as caught:
            _restore(facade, store)
        assert caught.value.code == "HARNESS_PERSISTENCE_FAILED"
        deadline = time.monotonic() + 3
        while actor._restore_batches:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        by_name = {item.name: item.status for item in store.load().harnesses}
        assert by_name == {"alpha": "running", "beta": "failed"}
        with events._condition:
            terminal = [
                event for event in events._events
                if isinstance(event, (HarnessRestoreCompleted, PortCommandRejected))
            ]
        assert len(terminal) == 1
        assert isinstance(terminal[0], PortCommandRejected)
        assert terminal[0].correlation_id
    finally:
        assert actor.close_runtime(3.0)


def test_restore_without_desired_store_keeps_original_failure_alarm() -> None:
    actor = HarnessRuntimeActor(_Launcher(start_failures=1), failure_budget=3)
    facade = HarnessRuntimeFacade(actor)
    try:
        summary = facade.restore((HarnessLaunchSpec("codex", "veteran", True),))
        assert summary.failed == 1
        assert facade.drain_failed_events() == ("codex:veteran",)
        assert [report.verdict for report in facade.drain_readiness_reports()] == ["failed"]
    finally:
        assert actor.close_runtime(3.0)


def test_close_does_not_reject_a_failed_restore_correlation_twice(tmp_path, monkeypatch):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    for name in ("alpha", "beta"):
        store.upsert_harness(HarnessLaunchSpec("codex", name, True))
    beta_entered, release = threading.Event(), threading.Event()

    class Launcher(_Launcher):
        def start(self, spec):
            if spec.name == "beta":
                beta_entered.set()
                assert release.wait(5)
            raise RuntimeError("synthetic failed restore")

    def failed_write(_harness, _name):
        raise OSError("synthetic failed write")

    monkeypatch.setattr(store, "mark_harness_failed", failed_write)
    events = _Events()
    actor = HarnessRuntimeActor(Launcher(), event_sink=events, desired_state=store)
    facade = HarnessRuntimeFacade(actor)
    try:
        with pytest.raises(RuntimeError) as caught:
            _restore(facade, store)
        assert caught.value.code == "HARNESS_PERSISTENCE_FAILED"
        assert beta_entered.wait(2)
        with events._condition:
            rejection = next(event for event in events._events if isinstance(event, PortCommandRejected))
        facade.stop(timeout=0.05)
        with events._condition:
            outcomes = [event for event in events._events
                        if isinstance(event, (PortCommandRejected, HarnessRestoreCompleted))
                        and event.correlation_id == rejection.correlation_id]
        assert outcomes == [rejection]
    finally:
        release.set()
        assert actor.close_runtime(3.0)


def test_last_deferred_target_cannot_complete_the_batch_twice(tmp_path, monkeypatch):
    store = DesiredStateStore(tmp_path / "desired-state.json")
    for name in ("alpha", "beta"):
        store.upsert_harness(HarnessLaunchSpec("codex", name, True))
    original = HarnessRuntimeActor._begin_start

    def claimed_before_start(self, key, record, *args, **kwargs):
        if key == "codex:beta":
            # Inject the same owner fact the lifecycle path can establish
            # after restore queues a key but before its start is admitted.
            self._lifecycle_effect_resources.add(key)
        return original(self, key, record, *args, **kwargs)

    monkeypatch.setattr(HarnessRuntimeActor, "_begin_start", claimed_before_start)
    events = _Events()
    actor = HarnessRuntimeActor(
        _Launcher(start_failures=1), event_sink=events,
        desired_state=store, start_max_workers=1,
    )
    facade = HarnessRuntimeFacade(actor)
    try:
        summary = _restore(facade, store)
        assert (summary.failed, summary.deferred) == (1, 1)
        facade.drain_readiness_reports()  # FIFO owner barrier after settlement
        with events._condition:
            outcomes = [event for event in events._events if isinstance(event, HarnessRestoreCompleted)]
        assert len(outcomes) == 1
    finally:
        assert actor.close_runtime(3.0)
