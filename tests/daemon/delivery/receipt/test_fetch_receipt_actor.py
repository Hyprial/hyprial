import threading
from types import SimpleNamespace

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.inbox.tracking.fetch_receipts import FetchReceiptPublisher


def test_slow_receipt_push_does_not_hold_fetch_caller():
    entered, release = threading.Event(), threading.Event()
    sent = []

    def put(key, payload):
        entered.set()
        assert release.wait(3)
        sent.append((key, payload))

    publisher = FetchReceiptPublisher(SimpleNamespace(put=put), capacity=1)
    message = SimpleNamespace(sender="agent:owner:node:sender", message_id="message")
    try:
        assert publisher.submit(message) is AdmissionResult.ACCEPTED
        assert entered.wait(1)
        assert publisher.submit(message) is AdmissionResult.ACCEPTED
        assert publisher.submit(message) is AdmissionResult.OVERLOADED
        assert not publisher.close(0.01)
    finally:
        release.set()
        assert publisher.close(2)
    assert len(sent) == 2
    assert publisher.projection().rejected == 1


def test_failed_hint_is_visible_and_next_receipt_still_runs():
    def put(key, payload):
        raise OSError("native send unavailable")

    publisher = FetchReceiptPublisher(SimpleNamespace(put=put))
    publisher.submit(SimpleNamespace(sender="sender", message_id="message"))
    assert publisher.close(2)
    assert publisher.projection().failed == 1
    assert publisher.projection().published == 0


def test_failed_hint_retains_original_diagnostic_event():
    events = []

    def put(_key, _payload):
        raise OSError("native send unavailable")

    publisher = FetchReceiptPublisher(
        SimpleNamespace(put=put),
        logger=lambda level, component, event, **fields: events.append(
            (level, component, event, fields)
        ),
    )
    publisher.submit(SimpleNamespace(sender="sender", message_id="message"))
    assert publisher.close(2)
    assert events == [
        ("warn", "inbox", "inbox.fetch_receipt.publish_failed", {
            "messageId": "message", "skipped": 0,
            "errorType": "OSError", "detail": "native send unavailable",
        }),
    ]
