import threading

from hyprial.daemon.impl.adapters.lark.worker.control import ControlIoOwner


def test_control_timeout_keeps_native_receipt_custody_and_bounds_next_admission():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def exchange(frame, delivery_id):
        entered.set()
        assert release.wait(3)
        calls.append((frame, delivery_id, threading.get_ident()))
        return True

    owner = ControlIoOwner(exchange, capacity=1, timeout=0.01)
    try:
        assert not owner.deliver({"text": "first"}, "one")
        assert entered.is_set()
        assert not owner.deliver({"text": "second"}, "two")
        assert not owner.close(0.01)
    finally:
        release.set()
        assert owner.close(2)
    assert len(calls) == 1
    assert calls[0][2] != threading.get_ident()


def test_concurrent_control_exchanges_never_share_native_socket():
    threads = []
    owner = ControlIoOwner(
        lambda frame, delivery_id: threads.append(threading.get_ident()) or True
    )
    outcomes = []
    callers = [
        threading.Thread(
            target=lambda: outcomes.append(owner.deliver({"text": "x"}, "id"))
        )
        for _ in range(4)
    ]
    try:
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(2)
        assert outcomes == [True] * 4
        assert len(set(threads)) == 1
    finally:
        assert owner.close()
