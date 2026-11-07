import threading

from hyprial.kernel.impl.actor_runtime import ActorRuntime, ActorSpec


def test_repeated_bounded_close_keeps_one_backend_stop_request():
    entered, release = threading.Event(), threading.Event()

    def handler(command):
        entered.set()
        assert release.wait(3)

    runtime = ActorRuntime()
    handle = runtime.start(
        ActorSpec("stop-custody", lambda: handler, mailbox_capacity=1)
    )
    runtime.tell(handle, "held")
    assert entered.wait(1)
    endpoint = runtime._guardian._children[handle.actor_id].endpoint
    try:
        for _ in range(100):
            assert not runtime.stop(handle, timeout=0)
        assert endpoint.ref.actor_inbox.qsize() == 1
    finally:
        release.set()
        assert runtime.stop(handle, timeout=2)
