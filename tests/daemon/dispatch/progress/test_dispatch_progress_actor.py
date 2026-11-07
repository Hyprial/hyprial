from __future__ import annotations

import threading

from hyprial.kernel import AdmissionResult
from hyprial.daemon.impl.dispatch.runtime.progress import ProgressPublishCoordinator
from hyprial.daemon.impl.inbox.tracking.progress import ProgressEvent


def _event(delivery_id: str) -> ProgressEvent:
    return ProgressEvent(
        delivery_id=delivery_id,
        conversation_id=f"conversation-{delivery_id}",
        actor="worker",
        harness="pi",
        phase="working",
        seq=1,
        emitted_at_ms=1,
        summary="started",
    )


def test_blocked_progress_send_does_not_hold_unrelated_publication() -> None:
    entered = threading.Event()
    release = threading.Event()
    second = threading.Event()

    def submit(event: ProgressEvent, *, recipient: str) -> bool:
        assert recipient == "sender"
        if event.delivery_id == "one":
            entered.set()
            assert release.wait(2)
        else:
            second.set()
        return True

    owner = ProgressPublishCoordinator(submit, workers=2)
    assert owner.submit(_event("one"), "sender") is AdmissionResult.ACCEPTED
    assert entered.wait(1)
    assert owner.submit(_event("two"), "sender") is AdmissionResult.ACCEPTED
    assert second.wait(1)
    assert not owner.close(0.02)
    release.set()
    assert owner.close(2)
    assert owner.projection().published == 2
