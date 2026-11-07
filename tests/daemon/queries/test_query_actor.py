import threading

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.transport.query_actor import QueryIoOwner


class Query:
    selector = "key"
    payload = b"payload"

    def __init__(self):
        self.replies = []
        self.errors = []

    def reply(self, key, payload, **options):
        self.replies.append((key, payload, threading.get_ident()))

    def reply_err(self, payload):
        self.errors.append(payload)


def test_slow_query_keeps_rx_admission_bounded_and_close_retains_handle():
    entered, release = threading.Event(), threading.Event()

    def handler(selector, payload):
        entered.set()
        assert release.wait(3)
        yield selector, payload

    owner = QueryIoOwner("test", handler, capacity=1)
    query = Query()
    try:
        assert owner.admit(query) is AdmissionResult.ACCEPTED
        assert entered.wait(1)
        assert owner.admit(Query()) is AdmissionResult.OVERLOADED
        assert owner.projection().outstanding == 1
        assert not owner.close(0.01)
        assert query.replies == []
    finally:
        release.set()
        assert owner.close(2)
    assert query.replies[0][:2] == ("key", b"payload")
    assert query.replies[0][2] != threading.get_ident()
    assert owner.projection().rejected == 1


def test_query_error_is_reported_without_killing_io_owner():
    def handler(selector, payload):
        raise OSError("synthetic private payload")

    owner = QueryIoOwner("failed", handler)
    query = Query()
    assert owner.admit(query) is AdmissionResult.ACCEPTED
    assert owner.close(2)
    assert query.errors == [b"QUERY_HANDLER_FAILED"]
    assert owner.projection().failed == 1
