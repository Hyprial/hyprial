"""Harness argument validation is permanent and budget-free."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.harnesses.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.runtime.ports import (
    EnsureHarnessCommand,
    HarnessLaunchProjection,
)
from hyprial.daemon.impl.lifecycle.lifecycle_receipts import LifecycleMutationFailed
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime
from hyprial.kernel import (
    HarnessLaunchSpec,
    LifecycleMutationRequest,
    PortAdmission,
    ipc_errors,
)

from tests.daemon.harnesses.actors.authority.support import (
    WAIT_TIMEOUT_SECONDS,
    _Launcher,
    _wait_for,
)


def _invalid_spec(tmp_path) -> HarnessLaunchSpec:  # type: ignore[no-untyped-def]
    return HarnessLaunchSpec(
        "claude",
        "worker",
        True,
        args=("--dangerously-skip-permissions", "--json"),
        cwd=str(tmp_path),
    )


def test_explicit_invalid_args_do_not_spend_budget_or_emit_alarm(tmp_path) -> None:
    launcher = _Launcher()
    runtime = ManagedHarnessRuntime(
        launcher, failure_budget=3, restart_backoff_seconds=0.0
    )
    try:
        with pytest.raises(RuntimeError) as failure:
            runtime.start(_invalid_spec(tmp_path))
        assert getattr(failure.value, "code", None) == (
            ipc_errors.HARNESS_ARGS_INVALID
        )
        record = runtime._actor._records["claude:worker"]
        assert record.failures == 0
        assert runtime.status()[0]["state"] == "failed"
        assert launcher.processes == []
        assert runtime.drain_failed_events() == ()
        for _ in range(3):
            runtime.reconcile()
        assert launcher.processes == []
    finally:
        runtime.stop()


def test_lifecycle_invalid_args_skip_retry_counter(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    counted: list[tuple[str, str]] = []
    original = store.record_harness_lifecycle_failure
    monkeypatch.setattr(
        store,
        "record_harness_lifecycle_failure",
        lambda attempt, resource: (
            counted.append((attempt, resource))
            or original(attempt, resource)
        ),
    )
    events: list[object] = []
    actor = HarnessRuntimeActor(
        _Launcher(), desired_state=store, event_sink=events.append,
        failure_budget=3,
    )
    request = LifecycleMutationRequest(
        "invalid-lifecycle",
        "invalid-lifecycle:attempt",
        "invalid-lifecycle",
        None,
        EnsureHarnessCommand(
            "invalid-payload",
            HarnessLaunchProjection(
                "claude", "worker", True,
                args=("--dangerously-skip-permissions", "--json"),
                cwd=str(tmp_path),
            ),
        ),
    )
    try:
        assert actor.submit(request) is PortAdmission.ACCEPTED
        _wait_for(
            lambda: any(
                isinstance(event, LifecycleMutationFailed)
                for event in events
            ),
            WAIT_TIMEOUT_SECONDS,
        )
        failure = next(
            event for event in events
            if isinstance(event, LifecycleMutationFailed)
        )
        assert failure.code == ipc_errors.HARNESS_ARGS_INVALID
        assert counted == []
        assert actor._failed_events == []
    finally:
        assert actor.close_runtime(WAIT_TIMEOUT_SECONDS)


def test_restore_invalid_args_are_terminal_without_retry_alarm(tmp_path) -> None:
    launcher = _Launcher()
    runtime = ManagedHarnessRuntime(
        launcher, failure_budget=3, restart_backoff_seconds=0.0
    )
    try:
        summary = runtime.restore((_invalid_spec(tmp_path),))
        assert summary.attempted == 1
        assert summary.restored == 0
        assert summary.failed == 1
        record = runtime._actor._records["claude:worker"]
        assert record.failures == 0
        assert runtime.status()[0]["state"] == "failed"
        assert launcher.processes == []
        assert runtime.drain_failed_events() == ()
        for _ in range(3):
            runtime.reconcile()
        assert launcher.processes == []
    finally:
        runtime.stop()
