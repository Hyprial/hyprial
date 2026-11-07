from __future__ import annotations

import threading
import time
import pytest
from pathlib import Path
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.composition import CorrelatedDomainEvents, HarnessPortClient
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.actor.facade import HarnessRuntimeFacade
from hyprial.daemon.impl.harnesses.runtime.ports import EnsureHarnessCommand, HarnessLaunchProjection, RemoveHarnessCommand
from hyprial.daemon.impl.lifecycle.lifecycle_receipts import LifecycleMutationFailed
from hyprial.kernel import LifecycleMutationCompleted
from hyprial.kernel import LifecycleMutationRequest

from ..support import (
    PER_STEP_WAIT_SECONDS,
    TERMINAL_WAIT_SECONDS,
    _Events,
    _Launcher,
    _Process,
    _request,
)


def test_harness_ensure_failure_control_waits_for_late_process_stop(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    first_control_seen = threading.Event()
    second_control_seen = threading.Event()
    controls: list[str] = []

    class _BlockingLauncher(_Launcher):
        def start(self, spec: HarnessLaunchSpec) -> _Process:
            self.start_calls += 1
            entered.set()
            assert release.wait(PER_STEP_WAIT_SECONDS)
            process = _Process()
            self.processes.append(process)
            return process

    class _ObservedActor(HarnessRuntimeActor):
        def _on_fail_lifecycle(self, command: object) -> None:
            controls.append(str(getattr(command, "correlation_id", "")))
            (first_control_seen if len(controls) == 1 else second_control_seen).set()
            super()._on_fail_lifecycle(command)

    store = DesiredStateStore(tmp_path / "desired.json")
    events = CorrelatedDomainEvents()
    launcher = _BlockingLauncher()
    actor = _ObservedActor(
        launcher,
        event_sink=events,
        desired_state=store,
        start_timeout_seconds=PER_STEP_WAIT_SECONDS / 10,
    )
    client = HarnessPortClient(actor, events)
    request = _request(
        "fenced-start",
        0,
        EnsureHarnessCommand(
            "fenced-start-payload",
            HarnessLaunchProjection("codex", "fenced-start", True),
        ),
    )

    try:
        assert actor.submit(request) is PortAdmission.ACCEPTED
        assert entered.wait(PER_STEP_WAIT_SECONDS)
        deadline = time.monotonic() + TERMINAL_WAIT_SECONDS
        while launcher.start_calls < 2:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        with pytest.raises(TimeoutError, match="settlement unresolved"):
            client.fail_lifecycle(
                request,
                "LIFECYCLE_OPERATION_TIMEOUT",
                "test deadline",
                PER_STEP_WAIT_SECONDS / 10,
            )
        assert first_control_seen.is_set()
        assert not launcher.processes

        settled: list[object] = []
        failure_thread = threading.Thread(
            target=lambda: settled.append(
                client.fail_lifecycle(
                    request,
                    "LIFECYCLE_OPERATION_TIMEOUT",
                    "test deadline",
                    PER_STEP_WAIT_SECONDS,
                )
            ),
            daemon=True,
        )
        failure_thread.start()
        assert second_control_seen.wait(PER_STEP_WAIT_SECONDS)
        assert failure_thread.is_alive()

        release.set()
        failure_thread.join(PER_STEP_WAIT_SECONDS)
        assert not failure_thread.is_alive()
        assert len(settled) == 1
        assert isinstance(settled[0], LifecycleMutationFailed)
        assert len(launcher.processes) == 2
        assert all(process.stop_calls == 1 for process in launcher.processes)
        assert not any(process.running for process in launcher.processes)
        assert actor.read_harness("codex:fenced-start") is None
    finally:
        release.set()
        assert actor.close_runtime(TERMINAL_WAIT_SECONDS)


def test_harness_ensure_late_ready_wins_before_failure_control(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class _BlockingLauncher(_Launcher):
        def start(self, spec: HarnessLaunchSpec) -> _Process:
            self.start_calls += 1
            entered.set()
            assert release.wait(PER_STEP_WAIT_SECONDS)
            process = _Process()
            self.processes.append(process)
            return process

    store = DesiredStateStore(tmp_path / "desired.json")
    events = CorrelatedDomainEvents()
    launcher = _BlockingLauncher()
    actor = HarnessRuntimeActor(
        launcher,
        event_sink=events,
        desired_state=store,
        start_timeout_seconds=0,
    )
    client = HarnessPortClient(actor, events)
    request = _request(
        "late-ready-wins",
        0,
        EnsureHarnessCommand(
            "late-ready-wins-payload",
            HarnessLaunchProjection("codex", "late-ready-wins", True),
        ),
    )

    try:
        assert actor.submit(request) is PortAdmission.ACCEPTED
        assert entered.wait(PER_STEP_WAIT_SECONDS)
        release.set()
        completed = events.wait(
            request.correlation_id,
            LifecycleMutationCompleted,
            timeout=TERMINAL_WAIT_SECONDS,
        )
        settled = client.fail_lifecycle(
            request,
            "LIFECYCLE_OPERATION_TIMEOUT",
            "test deadline",
            PER_STEP_WAIT_SECONDS,
        )
        assert isinstance(settled, LifecycleMutationCompleted)
        assert settled.provenance == completed.provenance
        assert settled.payload.harness_id == completed.payload.harness_id
        assert launcher.processes[0].running
        assert launcher.processes[0].stop_calls == 0
    finally:
        release.set()
        assert actor.close_runtime(TERMINAL_WAIT_SECONDS)


def test_harness_terminal_rollback_cannot_clobber_external_replacement(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class _BlockedFailureLauncher:
        def start(self, _spec: HarnessLaunchSpec) -> _Process:
            entered.set()
            assert release.wait(PER_STEP_WAIT_SECONDS)
            raise RuntimeError("permanent blocked failure")

    store = DesiredStateStore(tmp_path / "desired.json")
    events = _Events()
    actor = HarnessRuntimeActor(
        _BlockedFailureLauncher(),
        event_sink=events,
        desired_state=store,
        failure_budget=1,
    )
    spec = HarnessLaunchProjection("codex", "replacement", True)
    ensure = _request(
        "replacement",
        0,
        EnsureHarnessCommand("lifecycle:replacement:forward:0", spec),
    )
    assert actor.submit(ensure) is PortAdmission.ACCEPTED
    assert entered.wait(PER_STEP_WAIT_SECONDS)
    store.upsert_harness(
        HarnessLaunchSpec("codex", "replacement", True, args=("--new",))
    )
    release.set()
    failed = events.wait_failed(ensure.correlation_id, TERMINAL_WAIT_SECONDS)
    assert not failed.rolled_back
    assert store.load().harnesses[0].args == ("--new",)
    assert actor.close_runtime(TERMINAL_WAIT_SECONDS)


def test_harness_close_keeps_removed_spec_absent_and_preserves_replacement(
    tmp_path: Path,
) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    original = HarnessLaunchSpec("codex", "remove-close", True, args=("--old",))
    store.upsert_harness(original)
    events = _Events()
    launcher = _Launcher(stop_failures=100)
    actor = HarnessRuntimeActor(
        launcher,
        event_sink=events,
        desired_state=store,
        failure_budget=3,
        lifecycle_retry_base_seconds=1.0,
    )
    HarnessRuntimeFacade(actor).restore((original,))
    remove = _request(
        "remove-close",
        0,
        RemoveHarnessCommand(
            "lifecycle:remove-close:forward:0", "codex", "remove-close"
        ),
    )
    assert actor.submit(remove) is PortAdmission.ACCEPTED
    deadline = time.monotonic() + PER_STEP_WAIT_SECONDS
    while True:
        receipts = store.load().lifecycle_receipts
        if receipts and receipts[0].attempts == 1:
            break
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert actor.close_runtime(TERMINAL_WAIT_SECONDS)
    # Allen 2026-09-07: failed down preserves absent intent, not rollback.
    assert not events.wait_failed(remove.correlation_id).rolled_back
    state = store.load()
    assert not state.harnesses
    assert not state.lifecycle_receipts

    # Own state dir: since U0a-2 the desired-state authority is one SQLite
    # document PER STATE DIR, so a second independent custody needs its own
    # directory, not just a different json filename in the same one.
    replacement_store = DesiredStateStore(
        tmp_path / "replacement-state" / "desired-state.json"
    )
    replacement_events = _Events()
    replacement_actor = HarnessRuntimeActor(
        _Launcher(start_failures=100),
        event_sink=replacement_events,
        desired_state=replacement_store,
        failure_budget=3,
        lifecycle_retry_base_seconds=1.0,
    )
    ensure = _request(
        "close-replacement",
        0,
        EnsureHarnessCommand(
            "lifecycle:close-replacement:forward:0",
            HarnessLaunchProjection("codex", "replacement", True),
        ),
    )
    assert replacement_actor.submit(ensure) is PortAdmission.ACCEPTED
    deadline = time.monotonic() + PER_STEP_WAIT_SECONDS
    while True:
        receipts = replacement_store.load().lifecycle_receipts
        if receipts and receipts[0].attempts == 1:
            break
        assert time.monotonic() < deadline
        time.sleep(0.005)
    replacement = HarnessLaunchSpec(
        "codex", "replacement", True, args=("--external",)
    )
    replacement_store.upsert_harness(replacement)
    assert replacement_actor.close_runtime(TERMINAL_WAIT_SECONDS)
    failed = replacement_events.wait_failed(ensure.correlation_id)
    assert not failed.rolled_back
    state = replacement_store.load()
    assert state.harnesses == (replacement,)
    assert not state.lifecycle_receipts


def test_fresh_close_uses_persisted_arbitrary_correlation_and_is_idempotent(
    tmp_path: Path,
) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    request = LifecycleMutationRequest(
        "arbitrary-correlation-not-derived-from-attempt",
        "attempt-with-no-correlation-shape",
        "operation",
        None,
        EnsureHarnessCommand(
            "payload-correlation",
            HarnessLaunchProjection("codex", "fresh-close", True),
        ),
    )
    provenance, replayed = store.apply_harness_lifecycle(request)
    assert provenance.changed and not replayed
    events = _Events()
    actor = HarnessRuntimeActor(_Launcher(), event_sink=events, desired_state=store)
    assert actor.close_runtime(TERMINAL_WAIT_SECONDS)
    failed = events.wait_failed(request.correlation_id)
    assert failed.attempt_token == request.attempt_token
    assert failed.rolled_back
    assert actor.close_runtime(TERMINAL_WAIT_SECONDS)
    assert sum(
        isinstance(item, LifecycleMutationFailed)
        and item.correlation_id == request.correlation_id
        for item in events._events
    ) == 1
    assert not store.load().lifecycle_receipts


def test_paused_retry_callback_after_close_cannot_rewrite_or_start(
    tmp_path: Path,
) -> None:
    paused = threading.Event()
    finished = threading.Event()
    release = threading.Event()

    class _PausedRetryActor(HarnessRuntimeActor):
        def _emit_completion(self, completion: object):  # type: ignore[no-untyped-def]
            if isinstance(completion, LifecycleMutationRequest):
                paused.set()
                # This safety net spans the main thread's close_runtime
                # (operation-level work), so it keeps the terminal budget,
                # unlike the other fakes' release nets.
                assert release.wait(TERMINAL_WAIT_SECONDS)
                try:
                    return super()._emit_completion(completion)
                finally:
                    # The retry callback's full extent is now observable:
                    # once finished is set, no thread can still be on its
                    # way to a start, so the start_calls assertion below
                    # needs no wall-clock window at all.
                    finished.set()
            return super()._emit_completion(completion)

    store = DesiredStateStore(tmp_path / "desired.json")
    events = _Events()
    launcher = _Launcher(start_failures=100)
    actor = _PausedRetryActor(
        launcher,
        event_sink=events,
        desired_state=store,
        failure_budget=3,
        lifecycle_retry_base_seconds=0.01,
    )
    request = _request(
        "paused-close",
        0,
        EnsureHarnessCommand(
            "lifecycle:paused-close:forward:0",
            HarnessLaunchProjection("codex", "paused-close", True),
        ),
    )
    assert actor.submit(request) is PortAdmission.ACCEPTED
    assert paused.wait(PER_STEP_WAIT_SECONDS)
    assert actor.close_runtime(TERMINAL_WAIT_SECONDS)
    assert events.wait_failed(request.correlation_id).rolled_back
    release.set()
    assert finished.wait(PER_STEP_WAIT_SECONDS)
    assert launcher.start_calls == 1
    state = store.load()
    assert not state.harnesses
    assert not state.lifecycle_receipts
    assert actor.submit(request) is PortAdmission.CLOSING
    assert not store.load().harnesses
