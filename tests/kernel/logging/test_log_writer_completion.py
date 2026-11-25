"""A log observation joins only the accepted prefix it names."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.kernel.impl.log.writer import LogWriter


def test_exact_prefix_completes_before_later_blocked_write(actor_host) -> None:
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()

    def append(payload: bytes) -> None:
        if payload == b"first":
            first_entered.set()
            assert release_first.wait(3)
        else:
            second_entered.set()
            assert release_second.wait(3)

    writer = LogWriter(append, capacity=2, actor_host=actor_host)
    try:
        assert writer.submit(b"first") is AdmissionResult.ACCEPTED
        assert first_entered.wait(1)
        captured = writer.status()
        target = captured.accepted
        assert target == 1
        with ThreadPoolExecutor(max_workers=1) as pool:
            completion = pool.submit(
                writer.wait_for_writes, target=target,
                rejected_at_capture=captured.rejected, timeout=2,
            )
            assert writer.submit(b"second") is AdmissionResult.ACCEPTED
            release_first.set()
            assert second_entered.wait(1)
            assert completion.result(timeout=1) is True
            assert writer.status().accepted == 2
            assert writer.status().written == 1
    finally:
        release_first.set()
        release_second.set()
        assert writer.close(2)


def test_later_failure_or_refusal_does_not_rewrite_completed_prefix(actor_host) -> None:
    def append(payload: bytes) -> None:
        if payload == b"later":
            raise OSError("private path must not escape")

    writer = LogWriter(append, capacity=2, actor_host=actor_host)
    try:
        assert writer.submit(b"first") is AdmissionResult.ACCEPTED
        assert writer.wait_for_writes(target=1, rejected_at_capture=0, timeout=2)
        assert writer.submit(b"later") is AdmissionResult.ACCEPTED
        assert not writer.wait_for_writes(target=2, rejected_at_capture=0, timeout=2)
        assert writer.wait_for_writes(target=1, rejected_at_capture=0, timeout=2)
        status = writer.status()
        assert status.failed == 1 and status.last_error == "OSError"
        assert "private path" not in str(status)
    finally:
        assert writer.close(2)
    assert writer.submit(b"refused") is AdmissionResult.CLOSED
    assert writer.wait_for_writes(target=1, rejected_at_capture=0, timeout=0)
    assert not writer.wait_for_writes(target=1, rejected_at_capture=1, timeout=0)
    assert writer.status().rejected == 1
    second = LogWriter(lambda payload: None, actor_host=actor_host)
    assert second.submit(b"still usable") is AdmissionResult.ACCEPTED
    assert second.close()
