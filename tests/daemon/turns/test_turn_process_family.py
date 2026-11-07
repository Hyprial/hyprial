"""Turn-process family shape and Jev concurrency selection guards."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.harnesses import (
    BaseTurnProcess,
    ConcurrentTurnProcess,
    PythonHarnessProcess,
    SequentialTurnProcess,
    StreamingTurnProcess,
)


def test_legacy_streaming_name_is_the_sequential_family() -> None:
    assert StreamingTurnProcess is SequentialTurnProcess
    assert issubclass(SequentialTurnProcess, BaseTurnProcess)


def test_concurrent_family_validates_a_positive_bound() -> None:
    process = ConcurrentTurnProcess(concurrency=10)
    assert process.concurrency == 10
    with pytest.raises(ValueError, match="capacity"):
        ConcurrentTurnProcess(concurrency=0)


def test_jev_adapter_is_a_concurrent_turn_family_member() -> None:
    assert issubclass(PythonHarnessProcess, ConcurrentTurnProcess)
    assert issubclass(PythonHarnessProcess, BaseTurnProcess)
