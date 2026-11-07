import threading
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.transport.api import TransportSample
from hyprial.daemon.impl.transport.session_actor import TransportSessionAuthority


@dataclass
class Registration:
    closed: bool = False

    def close(self):
        self.closed = True


class Native:
    config = SimpleNamespace(connect=())

    def __init__(self):
        self.callbacks = []
        self.events = []

    def subscribe(self, key, callback):
        self.callbacks.append(callback)
        self.events.append("declare")
        return Registration()

    def put(self, key, payload):
        self.events.append(key)

    def reconfigure_connect(self, endpoints):
        self.events.append("rebuild")
        self.config = SimpleNamespace(connect=endpoints)

    def close(self):
        self.events.append("close")

    def declare_liveliness(self, key):
        self.events.append("declare_liveliness")
        if getattr(self, "fail_next_declare", False):
            self.fail_next_declare = False
            raise RuntimeError("session closed")
        return Registration()

    def session_health(self):
        return {"state": "open"}

    def get_liveliness(self, key, *, timeout=1.0):
        self.events.append(("liveliness", key, timeout))
        return []


def test_liveliness_query_uses_the_data_lane_and_keeps_late_custody():
    native = Native()
    entered, release = threading.Event(), threading.Event()

    def blocked(key, *, timeout):
        entered.set()
        assert release.wait(3)
        return [TransportSample(key, b"")]

    native.get_liveliness = blocked
    owner = TransportSessionAuthority(native, timeout=0.03)
    try:
        with pytest.raises(TimeoutError):
            owner.get_liveliness("peer", timeout=1.0)
        assert entered.is_set()
        with pytest.raises(TimeoutError):
            owner.close(0.01)
        release.set()
        owner.close(2)
        assert owner.projection().closed
    finally:
        release.set()
        owner.close(2)




def test_rebuild_fences_callbacks_already_queued_in_native_adapter():
    native = Native()
    owner = TransportSessionAuthority(native)
    received = []
    ready = threading.Event()

    def callback(sample):
        received.append(sample.key)
        ready.set()

    try:
        registration = owner.subscribe("key", callback)
        old_callback = native.callbacks[-1]
        owner.reconfigure_connect(("tcp/changed:1",))
        old_callback(TransportSample("old", b""))
        native.callbacks[-1](TransportSample("new", b""))
        assert ready.wait(1)
        assert received == ["new"]
        assert owner.projection().stale_callbacks == 1
        registration.close()
        native.callbacks[-1](TransportSample("retired", b""))
        assert received == ["new"]
    finally:
        owner.close()


def test_rebuild_completion_hook_observes_callbacks_complete_once():
    native = Native()
    owner = TransportSessionAuthority(native)
    observed = []
    stop = owner.on_rebuild_complete(
        lambda: observed.append(owner.projection().callbacks_complete)
    )
    try:
        owner.reconfigure_connect(("tcp/changed:1",))
        assert observed == [True]
        stop()
        owner.reconfigure_connect(("tcp/changed:2",))
        assert observed == [True]
    finally:
        owner.close()


def test_put_timeout_retains_custody_and_rebuild_waits_for_it():
    entered, release = threading.Event(), threading.Event()
    native = Native()

    def blocked(key, payload):
        if key == "blocked":
            entered.set()
            assert release.wait(3)
        native.events.append(key)

    native.put = blocked
    owner = TransportSessionAuthority(native, timeout=0.03)
    try:
        with pytest.raises(TimeoutError, match="remains accepted"):
            owner.put("blocked", b"payload")
        assert entered.is_set()
        # Another admitted operation can complete on an independent I/O lane.
        owner.put("free", b"payload")
        with pytest.raises(TimeoutError, match="remains accepted"):
            owner.reconfigure_connect(("tcp/new:1",))
        assert "rebuild" not in native.events
        with pytest.raises(TimeoutError):
            owner.close(0.01)
        with pytest.raises(RuntimeError, match="closed"):
            owner.put("after-close", b"")
        release.set()
        owner.close(2)
        assert native.events == ["free", "blocked", "rebuild", "close"]
    finally:
        release.set()
        owner.close(2)


def test_rebuild_failure_rolls_forward_callback_generation():
    native = Native()

    def fail(endpoints):
        raise OSError("native rolled back")

    native.reconfigure_connect = fail
    owner = TransportSessionAuthority(native)
    event = threading.Event()
    completed = []
    try:
        owner.subscribe("key", lambda sample: event.set())
        owner.on_rebuild_complete(lambda: completed.append(True))
        old = native.callbacks[-1]
        with pytest.raises(OSError, match="rolled back"):
            owner.reconfigure_connect(("invalid",))
        assert completed == []
        old(TransportSample("old", b""))
        assert not event.is_set()
        native.callbacks[-1](TransportSample("new", b""))
        assert event.wait(1)
    finally:
        owner.close()


def test_callback_exception_invalidates_history_without_losing_next_sample():
    import time

    native = Native()
    owner = TransportSessionAuthority(native, callback_capacity=2)
    received = []
    settled = threading.Event()

    def callback(sample):
        if sample.key == "poison":
            raise ValueError("malformed sample")
        received.append(sample.key)
        settled.set()

    try:
        owner.subscribe("key", callback)
        observed = native.callbacks[-1]
        observed(TransportSample("poison", b"bad"))
        observed(TransportSample("good", b"ok"))

        assert settled.wait(1)
        assert received == ["good"]
        assert not owner.projection().callbacks_complete
        generation = owner.projection().generation
        deadline = time.monotonic() + 3
        while (
            owner.projection().generation == generation
            or not owner.projection().callbacks_complete
        ):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert owner.projection().callbacks_complete
    finally:
        owner.close()


def test_query_preserves_all_replies_and_errors():
    native = Native()

    def query(key, payload, *, timeout, errors, all_replies):
        assert all_replies and payload == b"question"
        errors.append("remote error")
        return [TransportSample(key, b"one"), TransportSample(key, b"two")]

    native.query = query
    owner = TransportSessionAuthority(native)
    try:
        errors = []
        replies = owner.query("key", b"question", errors=errors, all_replies=True)
        assert [sample.payload for sample in replies] == [b"one", b"two"]
        assert errors == ["remote error"]
    finally:
        owner.close()


def test_real_loopback_query_and_liveliness_survive_rebuild():
    import socket
    import time
    from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport, LivelinessDirectory

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        endpoint = f"tcp/127.0.0.1:{sock.getsockname()[1]}"
    first = TransportSessionAuthority(ZenohTransport(ZenohConfig(listen=(endpoint,))))
    second = TransportSessionAuthority(ZenohTransport(ZenohConfig(connect=(endpoint,))))
    directory = LivelinessDirectory(second)
    registration = first.declare_query_handler(
        "actor-test/query", lambda selector, payload: [(selector, payload or b"empty")]
    )
    presence = first.declare_liveliness("hyprial/v1/liveliness/mailbox/test-node")
    try:
        deadline = time.monotonic() + 3
        while "test-node" not in directory.online_mailboxes():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert (
            second.query("actor-test/query", b"question", timeout=1)[0].payload
            == b"question"
        )
        second.reconfigure_connect((endpoint,))
        deadline = time.monotonic() + 3
        while "test-node" not in directory.online_mailboxes():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert (
            second.query("actor-test/query", b"after", timeout=1)[0].payload == b"after"
        )
    finally:
        directory.close()
        presence.close()
        registration.close()
        second.close()
        first.close()


def test_orgfs_query_handler_survives_responder_authority_rebuild():
    import socket
    import time
    from hyprial.daemon.impl.transport import KeySpace, ZenohConfig, ZenohTransport

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        endpoint = f"tcp/127.0.0.1:{sock.getsockname()[1]}"
    responder = TransportSessionAuthority(
        ZenohTransport(ZenohConfig(listen=(endpoint,)))
    )
    requester = TransportSessionAuthority(
        ZenohTransport(ZenohConfig(connect=(endpoint,)))
    )
    key = KeySpace().orgfs_sync("space-large", "node-responder")
    registration = responder.declare_query_handler(
        key, lambda selector, _payload: [(selector, b"sync-page")]
    )
    try:
        time.sleep(0.3)
        assert requester.query(key, b"before", timeout=1.0)
        responder._native._session.close()
        responder.put("reopen/trigger", b"wake")
        deadline = time.monotonic() + 3.0
        replies = []
        while time.monotonic() < deadline and not replies:
            replies = requester.query(key, b"after", timeout=0.2)
        assert [reply.payload for reply in replies] == [b"sync-page"]
    finally:
        registration.close()
        requester.close()
        responder.close()


def test_failed_native_close_can_retry_without_reopening_admission():
    native = Native()
    attempts = []

    def close():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("transient close error")

    native.close = close
    owner = TransportSessionAuthority(native)
    try:
        with pytest.raises(OSError, match="transient"):
            owner.close()
        with pytest.raises(RuntimeError, match="closed"):
            owner.put("new", b"no")
        owner.close()
        assert owner.projection().closed
        assert len(attempts) == 2
    finally:
        owner.close()


@pytest.mark.parametrize("automatic", [False, True])
def test_callback_overflow_invalidates_presence_until_history_rebuild(automatic):
    import time
    from hyprial.daemon.impl.transport import LivelinessDirectory

    native = Native()
    native.observe_liveliness = lambda key, callback, **kw: native.subscribe(
        key, callback
    )
    owner = TransportSessionAuthority(native, callback_capacity=1)
    directory = LivelinessDirectory(owner)
    entered, release = threading.Event(), threading.Event()
    original = directory._update

    def blocked(kind, sample):
        if sample.key.endswith("blocked"):
            entered.set()
            assert release.wait(3)
        original(kind, sample)

    directory._update = blocked
    callback = native.callbacks[1]
    prefix = "hyprial/v1/liveliness/mailbox/"
    try:
        callback(TransportSample(prefix + "present", b""))
        deadline = time.monotonic() + 1
        while "present" not in directory.online_mailboxes():
            assert time.monotonic() < deadline
            time.sleep(0.005)
        callback(TransportSample(prefix + "blocked", b""))
        assert entered.wait(1)
        callback(TransportSample(prefix + "queued", b""))
        callback(TransportSample(prefix + "present", b"", "delete"))
        assert not owner.projection().callbacks_complete
        assert directory.online_mailboxes() == ()
        old_generation = owner.projection().generation
        release.set()
        if automatic:
            deadline = time.monotonic() + 3
            while (
                owner.projection().generation == old_generation
                or not owner.projection().callbacks_complete
            ):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert "rebuild" not in native.events  # no session replacement needed
        else:
            owner.reconfigure_connect(("new",))
        assert owner.projection().callbacks_complete
        assert "present" not in directory.online_mailboxes()
    finally:
        release.set()
        directory.close()
        owner.close()


def test_presence_admission_loss_recovers_through_transport_history(monkeypatch):
    import time

    from hyprial.kernel.impl.actor_runtime import AdmissionResult
    from hyprial.daemon.impl.transport import LivelinessDirectory
    from hyprial.daemon.impl.transport.presence_actor import PresenceChanged

    native = Native()
    callbacks = {}
    history = set()
    actor_key = "hyprial/v1/liveliness/actor/*"

    def observe(key, callback, **_kwargs):
        callbacks[key] = callback
        registration = native.subscribe(key, callback)
        if key == actor_key:
            for identity in sorted(history):
                callback(TransportSample(actor_key[:-1] + identity, b""))
        return registration

    native.observe_liveliness = observe
    owner = TransportSessionAuthority(native)
    directory = LivelinessDirectory(owner)
    old_generation = owner.projection().generation
    original_tell = directory._authority._runtime.tell
    rejected = threading.Event()

    def reject_once(handle, command):
        if isinstance(command, PresenceChanged) and not rejected.is_set():
            rejected.set()
            return AdmissionResult.OVERLOADED
        return original_tell(handle, command)

    monkeypatch.setattr(directory._authority._runtime, "tell", reject_once)
    try:
        history.add("present")
        callbacks[actor_key](TransportSample(actor_key[:-1] + "present", b""))
        assert rejected.wait(1)
        deadline = time.monotonic() + 3
        while not (
            owner.projection().generation > old_generation
            and owner.projection().callbacks_complete
            and directory.actor_online("present")
        ):
            assert time.monotonic() < deadline, (
                owner.projection(), directory._authority.projection()
            )
            time.sleep(0.01)
        assert "rebuild" not in native.events
    finally:
        directory.close()
        owner.close()


def test_native_handoff_drop_invalidates_cached_presence_and_recovers():
    import time
    from hyprial.daemon.impl.transport import LivelinessDirectory, ZenohTransport

    native = Native()
    native._dispatcher = SimpleNamespace(_guard=threading.Lock(), _dropped={})
    native.callback_overflow_count = lambda: ZenohTransport.callback_overflow_count(
        native
    )
    native.observe_liveliness = lambda key, callback, **kw: native.subscribe(
        key, callback
    )
    owner = TransportSessionAuthority(native)
    directory = LivelinessDirectory(owner)
    try:
        native.callbacks[1](TransportSample("hyprial/v1/liveliness/mailbox/old", b""))
        deadline = time.monotonic() + 1
        while "old" not in directory.online_mailboxes():
            assert time.monotonic() < deadline
            time.sleep(0.005)
        native._dispatcher._dropped["mailbox"] = 1  # a lost native delete
        assert directory.online_mailboxes() == ()
        assert not owner.projection().callbacks_complete
        deadline = time.monotonic() + 3
        while not owner.projection().callbacks_complete:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert "old" not in directory.online_mailboxes()
        assert "rebuild" not in native.events
    finally:
        directory.close()
        owner.close()


def test_a_failed_reconfigure_reports_the_native_cause():
    """The actor is a courier for the native error: ``redial_failed`` logs it."""

    native = Native()
    owner = TransportSessionAuthority(native)
    cause = RuntimeError(
        "session rebuild failed (requested open: TimeoutError: dial timed out)"
    )

    def refuse(endpoints):
        raise cause

    native.reconfigure_connect = refuse
    try:
        with pytest.raises(RuntimeError) as caught:
            owner.reconfigure_connect(("tcp/changed:1",))
        assert caught.value is cause
        assert "requested open" in str(caught.value)
    finally:
        owner.close()


def test_callback_recovery_does_not_spin_while_the_session_is_closed(monkeypatch):
    """A closed/degraded native needs a reopen, not a once-a-second refresh.

    The refresh replays declarations onto the dead session, so it can never
    succeed; the transport's reopen loop is the only owner of that recovery.
    """

    native = Native()
    owner = TransportSessionAuthority(native)
    scheduled = []
    monkeypatch.setattr(
        owner._recovery_scheduler, "schedule", lambda *args: scheduled.append(args)
    )
    native.callback_overflow_count = lambda: 1
    try:
        native.session_health = lambda: {"state": "degraded"}
        owner.projection()  # a lost hand-off is observed, but not refreshed
        assert scheduled == []

        # Positive control: an open session with a lost hand-off still refreshes.
        native.session_health = lambda: {"state": "open"}
        native.callback_overflow_count = lambda: 2
        owner.projection()
        assert len(scheduled) == 1
    finally:
        owner.close()
