"""Behavior checks for relocated kernel execution contracts and primitives."""

from __future__ import annotations

import os
import threading

import pytest

from hyprial.kernel import (
    ActorRuntime,
    ActorSpec,
    AdmissionResult,
    CallCostCounters,
    DesiredStateError,
    HarnessLaunchSpec,
    OTHER_KEY,
    OwnedProcessGroup,
    ProcessLivenessProbeError,
    ProcessLivenessState,
)


def test_launch_spec_keeps_legacy_json_and_canonical_errors(monkeypatch) -> None:
    record = {
        "provider": "cc",
        "name": "worker",
        "headless": True,
        "args": ["--model", "sonnet"],
        "ownership": "managed",
    }
    spec = HarnessLaunchSpec.from_json(record, "harness")
    assert spec.harness == "claude"
    assert spec.to_json() == {**record, "provider": "claude"}
    monkeypatch.setattr("shutil.which", lambda _command: None)
    assert spec.resolved_command(("claude", "-p")) == ("claude", "-p")
    pinned = HarnessLaunchSpec("claude", "worker", True, command=("/bin/claude",))
    assert pinned.resolved_command(("claude", "-p")) == ("/bin/claude",)
    with pytest.raises(DesiredStateError, match=r"command\[0\] must be an absolute path"):
        HarnessLaunchSpec.from_json({**record, "command": ["relative"]}, "harness")


@pytest.mark.parametrize("identity", ["reused", None])
def test_process_birth_fence_is_loud_or_unknown(monkeypatch, identity) -> None:
    group = OwnedProcessGroup(label="test-owned")
    with group._lock:
        group._observed = True
        group._pid = 4242
        group._identity = "birth"
    monkeypatch.setattr(os, "killpg", lambda *_args: None)
    monkeypatch.setattr(group, "_process_birth_identity", lambda _pid: identity)
    if identity is None:
        observation = group.liveness()
        assert observation.state is ProcessLivenessState.UNKNOWN
        assert observation.observed is True
        assert observation.marker == "birth"
    else:
        with pytest.raises(ProcessLivenessProbeError, match="identity changed"):
            group.liveness()


def test_process_probe_permission_failure_uses_canonical_error(monkeypatch) -> None:
    group = OwnedProcessGroup(label="test-owned")
    assert group.liveness().state is ProcessLivenessState.UNKNOWN
    with group._lock:
        group._observed = True
        group._pid = 4242
        group._identity = "birth"

    def denied(*_args) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(os, "killpg", denied)
    with pytest.raises(ProcessLivenessProbeError, match="denied"):
        group.liveness()


def test_relocated_actor_backend_retains_admission_and_drain() -> None:
    entered = threading.Event()
    release = threading.Event()
    handled = []

    def handle(command) -> None:
        handled.append(command)
        if command == "first":
            entered.set()
            assert release.wait(2.0)

    runtime = ActorRuntime()
    actor = runtime.start(
        ActorSpec(name="kernel-bounded", handler_factory=lambda: handle, mailbox_capacity=1)
    )
    try:
        assert runtime.tell(actor, "first") is AdmissionResult.ACCEPTED
        assert entered.wait(2.0)
        assert runtime.tell(actor, "second") is AdmissionResult.ACCEPTED
        assert runtime.tell(actor, "third") is AdmissionResult.OVERLOADED
    finally:
        release.set()
        report = runtime.drain(2.0)
    assert report.complete is True
    assert handled == ["first", "second"]
    assert runtime.tell(actor, "after-drain") is AdmissionResult.CLOSED


def test_cost_counters_retain_bounded_unknown_bucket_and_wall_observations() -> None:
    counters = CallCostCounters(["known"], wall=True)
    counters.record("known", cpu_seconds=0.002, wall_seconds=0.05)
    counters.record("unknown", cpu_seconds=-1.0, wall_seconds=0.2, error=True)
    counters.record("another-unknown", cpu_seconds=0.001, wall_seconds=0.0005)
    snapshot = counters.snapshot()
    assert set(snapshot) == {"known", OTHER_KEY}
    assert snapshot["known"]["threadCpuMs"] == 2.0
    assert snapshot["known"]["lt100ms"] == 1
    assert snapshot[OTHER_KEY]["calls"] == 2
    assert snapshot[OTHER_KEY]["errors"] == 1
    assert snapshot[OTHER_KEY]["threadCpuMs"] == 1.0
    assert snapshot[OTHER_KEY]["ge100ms"] == 1
