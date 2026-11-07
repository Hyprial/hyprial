import threading
from types import SimpleNamespace

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.inbox import InboxMessage, DeliveryLifecycle
from hyprial.daemon.impl.inbox.actor import (
    DeliveryIoWorker,
    DispatchIoRequested,
    DispatchIoKind,
    DispatchItem,
)


def test_blocked_recipient_preserves_its_fifo_without_blocking_another():
    entered, release, second = threading.Event(), threading.Event(), threading.Event()
    calls, completions = [], []

    def deliver(message):
        calls.append(message.message_id)
        if message.message_id == "a1":
            entered.set()
            assert release.wait(3)
        return True

    def complete(request, result):
        completions.append(request.correlation_id)
        if request.correlation_id == "b":
            second.set()
        return AdmissionResult.ACCEPTED

    worker = DeliveryIoWorker(
        SimpleNamespace(is_online=lambda recipient: True, deliver=deliver),
        complete,
        lambda *args: False,
        capacity=3,
    )

    def request(identity, recipient):
        message = InboxMessage(
            message_id=identity,
            conversation_id="c",
            sender="sender",
            recipient=recipient,
            payload=b"body",
            intent="request",
            lifecycle=DeliveryLifecycle.ONLINE_ONLY,
            idempotency_key=identity,
            created_at_ms=1,
        )
        return DispatchIoRequested(
            identity, 1, 1, DispatchIoKind.DELIVERY, items=(DispatchItem(message),)
        )

    try:
        assert worker.submit(request("a1", "recipient-a"))
        assert entered.wait(1)
        assert worker.submit(request("a2", "recipient-a"))
        assert worker.submit(request("b", "recipient-b"))
        assert second.wait(1)
        assert "a2" not in calls
        assert not worker.drain(0.01)
    finally:
        release.set()
        assert worker.drain(2)
    assert calls.index("a1") < calls.index("a2")
    assert sorted(completions) == ["a1", "a2", "b"]
