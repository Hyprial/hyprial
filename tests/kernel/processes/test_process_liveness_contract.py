from __future__ import annotations

import os

import pytest

import hyprial.daemon.impl.harnesses.codex.process as codex_harness
from hyprial.daemon.impl.api import ProcessLivenessProbeError
from hyprial.kernel import ProcessLivenessState
from hyprial.daemon.impl.harnesses.codex.process import _OwnedProcessGroup
from hyprial.daemon.impl.harnesses.streaming.process import StreamingTurnProcess


def _record_observation(
    group: _OwnedProcessGroup, *, pid: int = 4242, marker: str | None
) -> None:
    """Put the real ownership handle in a post-register observation state."""

    with group._lock:
        group._observed = True
        group._pid = pid
        group._identity = marker


def test_never_observed_process_group_is_unknown() -> None:
    observation = _OwnedProcessGroup().liveness()

    assert observation.state is ProcessLivenessState.UNKNOWN
    assert observation.observed is False
    assert observation.pid is None


def test_observed_process_without_identity_criterion_is_unknown() -> None:
    group = _OwnedProcessGroup()
    _record_observation(group, marker=None)

    observation = group.liveness()

    assert observation.state is ProcessLivenessState.UNKNOWN
    assert observation.observed is True
    assert observation.pid == 4242


def test_confirmed_missing_process_group_is_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = _OwnedProcessGroup()
    _record_observation(group, marker="birth")
    monkeypatch.setattr(
        os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
    )

    observation = group.liveness()

    assert observation.state is ProcessLivenessState.DEAD
    assert observation.observed is True
    assert observation.pid == 4242
    assert observation.marker == "birth"


def test_streaming_wrapper_without_os_criterion_stays_unknown_until_stopped() -> None:
    class Client:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def query(self, _prompt: str) -> None:
            return None

        async def receive_response(self):  # type: ignore[no-untyped-def]
            if False:
                yield None

        async def interrupt(self) -> None:
            return None

    process = StreamingTurnProcess(
        harness="pi",
        label="Pi",
        client_factory=Client,
        thread_name="test-liveness-streaming",
    )
    try:
        assert process.wait_ready(timeout=1.0)
        observation = process.liveness()
        assert observation.state is ProcessLivenessState.UNKNOWN
        assert observation.observed is True
    finally:
        process.stop()

    assert process.liveness().state is ProcessLivenessState.DEAD


def test_changed_process_identity_is_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = _OwnedProcessGroup()
    _record_observation(group, marker="birth")
    monkeypatch.setattr(os, "killpg", lambda *_args: None)
    monkeypatch.setattr(codex_harness, "_process_birth_identity", lambda _pid: "reused")

    with pytest.raises(ProcessLivenessProbeError, match="identity changed"):
        group.liveness()


def test_process_group_probe_failure_is_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = _OwnedProcessGroup()
    _record_observation(group, marker="birth")
    monkeypatch.setattr(
        os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(PermissionError("denied")),
    )

    with pytest.raises(ProcessLivenessProbeError, match="denied"):
        group.liveness()
