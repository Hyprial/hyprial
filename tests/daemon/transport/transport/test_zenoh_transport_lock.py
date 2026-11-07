"""The transport lock never spans a network call, and a stuck holder is named.

Production, 2026-09-27 (#890): 38 of 40 IPC client threads queued at
``with self._lock:`` in ``ZenohTransport.put`` while zenoh receive threads
sat in ``flume::Sender::send`` (a subscriber's bounded channel was full), the
maintenance tick stopped, and nothing was logged for 40 minutes.  The
faulthandler dump caps at 100 threads, so it could not show who held the lock.

These tests pin the four properties that would have kept that silent stall
from forming, each against a fake session so the blocking is deterministic:

* ``put`` and ``reconfigure_connect`` release the lock while the network call
  (``session.put`` / ``session.close`` + ``zenoh.open``) runs;
* a waiter gives up after a bounded wait and names the holder with its stack;
* a subscriber callback that blocks never blocks the thread zenoh delivers
  on, and a full hand-off queue is reported rather than silent.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.transport.zenoh import transport as transport_module
from hyprial.daemon.impl.transport.zenoh.config import ZenohConfig
from hyprial.daemon.impl.transport.zenoh.transport import ZenohTransport


class _FakeSample:
    def __init__(self, key: str, payload: bytes) -> None:
        self.key_expr = key
        self.kind = "put"
        self._payload = payload

    @property
    def payload(self) -> Any:
        payload = self._payload

        class _Bytes:
            def to_bytes(self) -> bytes:
                return payload

        return _Bytes()


@pytest.mark.parametrize("mode", ["empty", "timeout", "error"])
def test_liveliness_query_keeps_negative_and_failure_outcomes_distinct(mode):
    class Replies:
        def recv(self):
            if mode == "empty":
                raise RuntimeError("channel is empty and closed")
            if mode == "timeout":
                raise RuntimeError("receive timeout")
            return SimpleNamespace(ok=None, err="query rejected")

    calls = []

    def get(key, *, timeout):
        calls.append((key, timeout))
        return Replies()

    transport = ZenohTransport.__new__(ZenohTransport)
    transport._current_session = lambda *_args: SimpleNamespace(
        liveliness=lambda: SimpleNamespace(get=get)
    )
    if mode == "empty":
        assert transport.get_liveliness("live/key", timeout=0.25) == []
    else:
        with pytest.raises(TimeoutError if mode == "timeout" else RuntimeError):
            transport.get_liveliness("live/key", timeout=0.25)
    assert calls == [("live/key", 0.25)]


class _FakeQuery:
    def __init__(self) -> None:
        self.drop_count = 0

    def drop(self) -> None:
        self.drop_count += 1


class _FakeDeclaration:
    def __init__(self, session: _FakeSession, key: str, callback: Callable[[Any], None]) -> None:
        self.session = session
        self.key = key
        self.callback = callback

    def undeclare(self) -> None:
        self.session.subscribers.remove(self)


class _FakeInfo:
    def __init__(self, *, fail_declare: bool = False) -> None:
        self.fail_declare = fail_declare
        self.transport_callback: Callable[[Any], None] | None = None
        self.link_callback: Callable[[Any], None] | None = None

    def declare_transport_events_listener(
        self, callback: Callable[[Any], None], *, history: bool
    ) -> object:
        assert history is True
        if self.fail_declare:
            raise RuntimeError("native listener declaration unavailable")
        self.transport_callback = callback
        return object()

    def declare_link_events_listener(
        self, callback: Callable[[Any], None], *, history: bool
    ) -> object:
        assert history is True
        self.link_callback = callback
        return object()


class _FakeSession:
    """Stands in for ``zenoh.Session``; each hook can be made to block."""

    def __init__(self, name: str, *, fail_declare: bool = False) -> None:
        self.name = name
        self.puts: list[tuple[str, bytes]] = []
        self.subscribers: list[_FakeDeclaration] = []
        self.closed = False
        self.put_entered = threading.Event()
        self.put_release: threading.Event | None = None
        self.info = _FakeInfo(fail_declare=fail_declare)

    def zid(self) -> str:
        return self.name

    def put(self, key: str, payload: bytes, **_: Any) -> None:
        self.put_entered.set()
        if self.put_release is not None:
            assert self.put_release.wait(10), "test never released the blocked put"
        self.puts.append((key, payload))

    def declare_subscriber(self, key: str, callback: Callable[[Any], None]) -> _FakeDeclaration:
        declaration = _FakeDeclaration(self, key, callback)
        self.subscribers.append(declaration)
        return declaration

    def close(self) -> None:
        self.closed = True

    def deliver(self, key: str, payload: bytes) -> None:
        """What a zenoh receive thread does: call every matching callback."""

        for declaration in list(self.subscribers):
            declaration.callback(_FakeSample(key, payload))


class _Opener:
    """Replaces ``zenoh.open``; the Nth open can be made to block."""

    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []
        self.block_on: int | None = None
        self.fail_declare = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, _config: Any) -> _FakeSession:
        index = len(self.sessions)
        session = _FakeSession(f"session-{index}", fail_declare=self.fail_declare)
        if self.block_on == index:
            self.entered.set()
            assert self.release.wait(10), "test never released the blocked open"
        self.sessions.append(session)
        return session


@pytest.fixture
def opener(monkeypatch: pytest.MonkeyPatch) -> _Opener:
    opener = _Opener()
    monkeypatch.setattr(transport_module.zenoh, "open", opener)
    return opener


def _run(target: Callable[[], Any], *, name: str) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def body() -> None:
        try:
            target()
        except BaseException as error:  # noqa: BLE001 - surfaced to the test
            errors.append(error)

    thread = threading.Thread(target=body, name=name, daemon=True)
    thread.start()
    return thread, errors


def _lock_is_free(transport: ZenohTransport) -> bool:
    acquired = transport._lock.acquire(timeout=0.5)
    if acquired:
        transport._lock.release()
    return acquired


def test_put_releases_the_lock_while_the_network_send_runs(opener: _Opener) -> None:
    transport = ZenohTransport(ZenohConfig())
    session = opener.sessions[0]
    session.put_release = threading.Event()

    thread, errors = _run(lambda: transport.put("k/one", b"x"), name="blocked-put")
    assert session.put_entered.wait(5)

    # Red before the fix: put holds the RLock across session.put, so every
    # other publisher (and the rebuild) queues behind one slow send.
    assert _lock_is_free(transport), "the transport lock is held across session.put"

    session.put_release.set()
    thread.join(5)
    assert not errors
    assert session.puts == [("k/one", b"x")]


def test_reconfigure_releases_the_lock_while_the_session_reopens(opener: _Opener) -> None:
    transport = ZenohTransport(ZenohConfig())
    opener.block_on = 1  # the reopen inside reconfigure_connect

    thread, errors = _run(
        lambda: transport.reconfigure_connect(("tcp/127.0.0.1:7447",)),
        name="blocked-reopen",
    )
    assert opener.entered.wait(5)

    assert _lock_is_free(transport), "the transport lock is held across zenoh.open"

    # A put issued during the rebuild waits it out and lands on the NEW
    # session, never on the one being closed.
    put_thread, put_errors = _run(lambda: transport.put("k/during", b"y"), name="put-during-rebuild")
    time.sleep(0.2)
    assert opener.sessions[0].puts == []

    opener.release.set()
    thread.join(5)
    put_thread.join(5)
    assert not errors and not put_errors
    assert opener.sessions[1].puts == [("k/during", b"y")]
    assert opener.sessions[0].closed


def test_session_events_are_once_per_transition_and_outside_the_lock(
    opener: _Opener,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    transport_ref: list[ZenohTransport] = []

    def record(event: str, fields: dict[str, object]) -> None:
        if transport_ref:
            assert _lock_is_free(transport_ref[0]), f"{event} emitted under lock"
        events.append((event, fields))

    transport = ZenohTransport(ZenohConfig(), event_sink=record)
    transport_ref.append(transport)
    transport.reconfigure_connect(("tcp/127.0.0.1:7447",))
    transport.close()
    transport.close()

    session_events = [
        (event, fields["zid"], fields["reason"])
        for event, fields in events
        if event in {"zenoh.session.opened", "zenoh.session.closed"}
    ]
    assert session_events == [
        ("zenoh.session.opened", "session-0", "initial"),
        ("zenoh.session.closed", "session-0", "reconfigure"),
        ("zenoh.session.opened", "session-1", "reconfigure"),
        ("zenoh.session.closed", "session-1", "transport-close"),
    ]


def test_peer_diagnostics_leave_the_zenoh_callback_before_logging(
    opener: _Opener,
) -> None:
    sink_entered = threading.Event()
    release_sink = threading.Event()

    def blocking_sink(event: str, _fields: dict[str, object]) -> None:
        if event == "zenoh.link.event":
            sink_entered.set()
            assert release_sink.wait(10)

    transport = ZenohTransport(ZenohConfig(), event_sink=blocking_sink)
    callback = opener.sessions[0].info.link_callback
    assert callback is not None
    link = type(
        "Link",
        (),
        {
            "zid": "peer-zid",
            "src": "tcp/127.0.0.1:7447",
            "dst": "tcp/127.0.0.1:50000",
        },
    )()
    event = type("LinkEvent", (), {"kind": "PUT", "link": link})()

    callback_thread, errors = _run(lambda: callback(event), name="zenoh-link-callback")
    callback_thread.join(1)
    try:
        assert not callback_thread.is_alive(), "diagnostic logging blocked the callback"
        assert not errors
        assert sink_entered.wait(5)
    finally:
        release_sink.set()
        transport.close()


def _link_event(dst_port: int) -> Any:
    link = type(
        "Link",
        (),
        {
            "zid": "peer-zid",
            "src": "tcp/127.0.0.1:7447",
            "dst": f"tcp/127.0.0.1:{dst_port}",
        },
    )()
    return type("LinkEvent", (), {"kind": "PUT", "link": link})()


def test_peer_diagnostics_are_bounded_and_report_what_they_dropped(
    opener: _Opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transport_module, "_DIAGNOSTIC_QUEUE_CAPACITY", 2)
    sink_entered = threading.Event()
    release_sink = threading.Event()
    events: list[tuple[str, dict[str, object]]] = []

    def blocking_sink(event: str, fields: dict[str, object]) -> None:
        if event == "zenoh.link.event" and not sink_entered.is_set():
            sink_entered.set()
            assert release_sink.wait(10)
        events.append((event, fields))

    transport = ZenohTransport(ZenohConfig(), event_sink=blocking_sink)
    callback = opener.sessions[0].info.link_callback
    assert callback is not None
    try:
        callback(_link_event(50000))
        assert sink_entered.wait(5)
        # The logger is parked in the sink; two fit, the other three drop.
        flood, errors = _run(
            lambda: [callback(_link_event(50001 + index)) for index in range(5)],
            name="zenoh-link-flood",
        )
        flood.join(1)
        assert not flood.is_alive(), "a full diagnostic queue blocked the callback"
        assert not errors
    finally:
        release_sink.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(
        event == "zenoh.diagnostic.dropped" for event, _ in events
    ):
        time.sleep(0.01)
    transport.close()

    assert [fields["count"] for event, fields in events if event == "zenoh.diagnostic.dropped"] == [3]
    assert [event for event, _ in events].count("zenoh.link.event") == 3


def test_a_failing_diagnostic_sink_does_not_stop_later_events(
    opener: _Opener,
) -> None:
    seen: list[str] = []
    second = threading.Event()

    def failing_sink(event: str, fields: dict[str, object]) -> None:
        if event != "zenoh.link.event":
            return
        seen.append(str(fields["dst"]))
        if len(seen) == 1:
            raise RuntimeError("sink broke")
        second.set()

    transport = ZenohTransport(ZenohConfig(), event_sink=failing_sink)
    callback = opener.sessions[0].info.link_callback
    assert callback is not None
    try:
        callback(_link_event(50000))
        callback(_link_event(50001))
        assert second.wait(5), "the diagnostic thread died on a sink error"
    finally:
        transport.close()
    assert seen == ["tcp/127.0.0.1:50000", "tcp/127.0.0.1:50001"]


def test_a_failing_diagnostic_listener_never_breaks_the_transport(
    opener: _Opener,
) -> None:
    """Red before the guard: a raising declare_*_listener killed construction.

    The session is already open and the drain thread already started at that
    point, so the exception would leak both and take the daemon down over an
    optional diagnostic.  The transport must stay usable and say so once.
    """

    opener.fail_declare = True
    events: list[tuple[str, dict[str, object]]] = []
    transport = ZenohTransport(
        ZenohConfig(), event_sink=lambda event, fields: events.append((event, fields))
    )
    try:
        transport.put("k/one", b"x")
        assert opener.sessions[0].puts == [("k/one", b"x")]
    finally:
        transport.close()

    assert events[0][0] == "zenoh.diagnostic.unavailable"
    assert events[0][1] == {"reason": "declare_failed", "error": "RuntimeError"}
    assert opener.sessions[0].closed


def test_peer_up_and_down_events_are_emitted_once_with_endpoint_fields(
    opener: _Opener,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    transport = ZenohTransport(
        ZenohConfig(connect=("tcp/127.0.0.1:7447",)),
        event_sink=lambda event, fields: events.append((event, fields)),
    )
    transport.set_endpoint_peers({"peer-node": "tcp/127.0.0.1:7447"})
    info = opener.sessions[0].info
    assert info.transport_callback is not None
    assert info.link_callback is not None
    peer = type("Peer", (), {"zid": "peer-zid", "whatami": "peer"})()
    link = type(
        "Link",
        (),
        {
            "zid": "peer-zid",
            "src": "tcp/127.0.0.1:7447",
            "dst": "tcp/127.0.0.1:50000",
        },
    )()

    for kind in ("PUT", "DELETE"):
        info.transport_callback(
            type("TransportEvent", (), {"kind": kind, "transport": peer})()
        )
        info.link_callback(type("LinkEvent", (), {"kind": kind, "link": link})())

    deadline = time.monotonic() + 5
    while len(events) < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    health = transport.session_health()
    transport.close()
    peer_events = [
        item
        for item in events
        if item[0] in {"zenoh.transport.event", "zenoh.link.event"}
    ]
    assert health["counters"]["zenoh_link_delete"] == 1
    assert peer_events == [
        (
            "zenoh.transport.event",
            {"kind": "put", "zid": "peer-zid", "whatami": "peer"},
        ),
        (
            "zenoh.link.event",
            {
                "kind": "put",
                "zid": "peer-zid",
                "src": "tcp/127.0.0.1:7447",
                "dst": "tcp/127.0.0.1:50000",
                "peerNode": "peer-node",
                "localEphemeralPort": 50000,
                "peerMapPort": 7447,
            },
        ),
        (
            "zenoh.transport.event",
            {"kind": "delete", "zid": "peer-zid", "whatami": "peer"},
        ),
        (
            "zenoh.link.event",
            {
                "kind": "delete",
                "zid": "peer-zid",
                "src": "tcp/127.0.0.1:7447",
                "dst": "tcp/127.0.0.1:50000",
                "reason": "native-cause-unavailable",
                "ageMs": peer_events[3][1]["ageMs"],
                "peerNode": "peer-node",
                "localEphemeralPort": 50000,
                "peerMapPort": 7447,
                "zenohLinkDeleteCount": 1,
                "mapPeerRemapCount": 0,
            },
        ),
    ]


def test_link_delete_reason_distinguishes_local_session_close(
    opener: _Opener,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    transport = ZenohTransport(
        ZenohConfig(), event_sink=lambda event, fields: events.append((event, fields))
    )
    session = opener.sessions[0]
    callback = session.info.link_callback
    assert callback is not None
    link = type(
        "Link",
        (),
        {
            "zid": "peer-zid",
            "src": "tcp/127.0.0.1:7447",
            "dst": "tcp/127.0.0.1:50123",
        },
    )()
    callback(type("LinkEvent", (), {"kind": "PUT", "link": link})())
    original_close = session.close

    def close_with_delete() -> None:
        callback(type("LinkEvent", (), {"kind": "DELETE", "link": link})())
        original_close()

    session.close = close_with_delete  # type: ignore[method-assign]
    transport.close()
    deadline = time.monotonic() + 5
    while len(events) < 4 and time.monotonic() < deadline:
        time.sleep(0.01)

    deleted = [
        fields
        for event, fields in events
        if event == "zenoh.link.event" and fields["kind"] == "delete"
    ]
    assert deleted[0]["reason"] == "session_close:transport-close"


def test_map_peer_remaps_have_a_counter_separate_from_link_deletes(
    opener: _Opener,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    transport = ZenohTransport(
        ZenohConfig(),
        event_sink=lambda event, fields: events.append((event, fields)),
    )
    try:
        transport.set_endpoint_peers({"peer-node": "tcp/127.0.0.1:7447"})
        transport.set_endpoint_peers({"peer-node": "tcp/127.0.0.1:7447"})
        transport.set_endpoint_peers({"peer-node": "tcp/127.0.0.1:7557"})
    finally:
        transport.close()

    remaps = [
        fields
        for event, fields in events
        if event == "zenoh.forwarding.map_peer_remap"
    ]
    assert remaps == [
        {
            "peerNode": "peer-node",
            "previousEndpoint": "tcp/127.0.0.1:7447",
            "currentEndpoint": "tcp/127.0.0.1:7557",
            "mapPeerRemapCount": 1,
        }
    ]
    assert not [event for event, _fields in events if event == "zenoh.link.event"]


def test_a_waiter_gives_up_and_names_the_holder_with_its_stack(
    opener: _Opener, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HYPRIAL_ZENOH_LOCK_WAIT_SECONDS", "0.3")
    transport = ZenohTransport(ZenohConfig())
    held = threading.Event()
    release = threading.Event()

    def hold_the_transport_lock_here() -> None:
        with transport._lock:
            held.set()
            release.wait(10)

    holder, _ = _run(hold_the_transport_lock_here, name="lock-holder-x")
    assert held.wait(5)

    outcome: list[BaseException] = []
    waiter, errors = _run(lambda: transport.put("k/stuck", b"z"), name="waiting-put")
    waiter.join(3)
    try:
        # Red before the fix: the waiter is still parked on the RLock.
        assert not waiter.is_alive(), "put waited on the transport lock without a bound"
        outcome = errors
        assert len(outcome) == 1
        error = outcome[0]
        assert type(error).__name__ == "TransportLockTimeout"
        assert "k/stuck" in str(error)
        assert "lock-holder-x" in str(error)
        stalls = [r for r in caplog.records if getattr(r, "event", "") == "zenoh.lock.wait_exceeded"]
        assert stalls, "the bounded wait failed without a log record"
        assert "hold_the_transport_lock_here" in stalls[0].getMessage()
    finally:
        release.set()
        holder.join(5)


def test_lock_holder_reports_the_thread_age_and_stack(opener: _Opener) -> None:
    transport = ZenohTransport(ZenohConfig())
    assert transport.lock_holder() is None

    held = threading.Event()
    release = threading.Event()

    def sit_on_the_lock() -> None:
        with transport._lock:
            held.set()
            release.wait(10)

    holder, _ = _run(sit_on_the_lock, name="lock-holder-y")
    try:
        assert held.wait(5)
        time.sleep(0.1)
        report = transport.lock_holder()
        assert report is not None
        assert report["thread"] == "lock-holder-y"
        assert report["heldSeconds"] >= 0.1
        assert "sit_on_the_lock" in report["stack"]
    finally:
        release.set()
        holder.join(5)
    assert transport.lock_holder() is None


def test_a_blocked_subscriber_never_blocks_the_delivering_thread(opener: _Opener) -> None:
    transport = ZenohTransport(ZenohConfig())
    session = opener.sessions[0]
    release = threading.Event()
    seen: list[bytes] = []

    def slow_callback(sample: Any) -> None:
        release.wait(10)
        seen.append(sample.payload)

    transport.subscribe("k/**", slow_callback)

    delivery, _ = _run(lambda: session.deliver("k/a", b"1"), name="zenoh-rx")
    delivery.join(1)
    # Red before the fix: zenoh's own callback thread runs slow_callback, so
    # its bounded channel fills and the receive threads stop.
    assert not delivery.is_alive(), "the delivering thread is stuck in a subscriber callback"

    second, _ = _run(lambda: session.deliver("k/b", b"2"), name="zenoh-rx-2")
    second.join(1)
    assert not second.is_alive()

    release.set()
    deadline = time.monotonic() + 5
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == [b"1", b"2"], "hand-off must preserve per-subscription order"


def test_a_full_hand_off_queue_is_reported_not_silent(
    opener: _Opener, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HYPRIAL_ZENOH_CALLBACK_QUEUE_CAPACITY", "2")
    caplog.set_level(logging.ERROR, logger=transport_module.__name__)
    transport = ZenohTransport(ZenohConfig())
    session = opener.sessions[0]
    release = threading.Event()
    transport.subscribe("k/**", lambda _sample: release.wait(10))

    def flood() -> None:
        for index in range(8):
            session.deliver("k/flood", str(index).encode())

    delivery, _ = _run(flood, name="zenoh-rx-flood")
    delivery.join(2)
    try:
        assert not delivery.is_alive(), "a full hand-off queue blocked the delivering thread"
        overflow = [r for r in caplog.records if getattr(r, "event", "") == "zenoh.subscriber.overflow"]
        assert overflow, "samples were dropped without an error record"
        assert "k/**" in overflow[0].getMessage()
    finally:
        release.set()


def test_close_releases_a_callback_lane_that_was_full(
    opener: _Opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full queue must not make its fixed worker miss the stop sentinel."""

    monkeypatch.setenv("HYPRIAL_ZENOH_CALLBACK_QUEUE_CAPACITY", "1")
    transport = ZenohTransport(ZenohConfig())
    session = opener.sessions[0]
    entered = threading.Event()
    release = threading.Event()

    def blocked_callback(_sample: Any) -> None:
        entered.set()
        release.wait(10)

    transport.subscribe("k/**", blocked_callback)
    session.deliver("k/first", b"1")
    assert entered.wait(2)
    session.deliver("k/queued", b"2")
    worker = transport._dispatcher._threads[0]
    assert worker is not None and worker.is_alive()

    transport.close()
    release.set()
    worker.join(2)
    assert not worker.is_alive(), "full callback lane stayed parked after close"


def test_query_pool_drops_overflow_and_stops_every_worker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatcher = transport_module._QueryDispatcher(capacity=1, workers=1)
    entered = threading.Event()
    release = threading.Event()

    def blocked_handler(_query: _FakeQuery) -> None:
        entered.set()
        release.wait(5)

    deliver = dispatcher.wrap("k/query", blocked_handler)
    active = _FakeQuery()
    queued = _FakeQuery()
    overflow = _FakeQuery()
    with caplog.at_level(logging.ERROR, logger=transport_module.__name__):
        deliver(active)
        assert entered.wait(1.0)
        deliver(queued)
        started = time.monotonic()
        deliver(overflow)
        assert time.monotonic() - started < 0.2

    assert overflow.drop_count == 1
    assert "zenoh.queryable.overflow" in caplog.text
    release.set()
    dispatcher.stop()
    assert active.drop_count == 1
    assert queued.drop_count == 1
    assert all(not thread.is_alive() for thread in dispatcher._threads)


def test_a_condition_on_the_owned_lock_constructs_waits_and_notifies() -> None:
    """Python 3.14's Condition binds ``lock.locked`` at construction; the
    wrapper once lacked it and the production daemon could not start."""

    lock = transport_module._OwnedLock()
    condition = threading.Condition(lock)  # type: ignore[arg-type]
    assert lock.locked() is False
    ready = threading.Event()
    woke: list[bool] = []

    def waiter() -> None:
        with condition:
            ready.set()
            woke.append(condition.wait(5))
            # The wait restored ownership to this thread.
            assert lock.owner() is not None
            assert lock.owner()[1] == threading.current_thread().name

    thread = threading.Thread(target=waiter, name="condition-waiter", daemon=True)
    thread.start()
    assert ready.wait(5)
    with condition:
        # The waiter's wait released the lock, so this thread owns it now.
        assert lock.locked() is True
        condition.notify_all()
    thread.join(5)
    assert woke == [True]
    assert lock.locked() is False
    assert lock.owner() is None


class _FakeLiveliness:
    def __init__(self, session: _LivelySession) -> None:
        self._session = session

    def declare_token(self, key: str) -> Any:
        # zenoh hands the new token to local liveliness observers and, when
        # their callback channel is full, waits for the callback to take it.
        # Model the worst case: wait for every observer callback to return.
        for callback in list(self._session.observers):
            runner = threading.Thread(
                target=callback, args=(_FakeSample(key, b""),), name="zenoh-callback"
            )
            runner.start()
            runner.join()
        return _FakeDeclaration(self._session, key, lambda _sample: None)

    def declare_subscriber(self, key: str, callback: Callable[[Any], None], **_: Any) -> Any:
        self._session.observers.append(callback)
        declaration = _FakeDeclaration(self._session, key, callback)
        self._session.subscribers.append(declaration)
        return declaration


class _LivelySession(_FakeSession):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.observers: list[Callable[[Any], None]] = []

    def liveliness(self) -> _FakeLiveliness:
        return _FakeLiveliness(self)


def test_a_liveliness_callback_that_publishes_cannot_wedge_a_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production, 2026-09-27 08:49Z: the redial replayed a liveliness token
    while holding the transport lock; zenoh delivered it to orgfs's liveliness
    observer, which announced via put and waited for that same lock.  Before
    #891 that wait was unbounded (the 04:38Z outage); with only a bounded
    wait it still ground on, 30 s per sample, with the tick stuck in
    forwarding.timer."""

    monkeypatch.setenv("HYPRIAL_ZENOH_LOCK_WAIT_SECONDS", "2")
    sessions: list[_LivelySession] = []

    def open_session(_config: Any) -> _LivelySession:
        session = _LivelySession(f"session-{len(sessions)}")
        sessions.append(session)
        return session

    monkeypatch.setattr(transport_module.zenoh, "open", open_session)
    transport = ZenohTransport(ZenohConfig())
    announced: list[BaseException | None] = []

    def announce_on_peer_change(_sample: Any) -> None:
        try:
            transport.put("org/fs/announce/self", b"a")
            announced.append(None)
        except BaseException as error:  # noqa: BLE001 - recorded for the assert
            announced.append(error)

    transport.observe_liveliness("liveliness/actor/*", announce_on_peer_change)
    transport.declare_liveliness("liveliness/actor/self")

    started = time.monotonic()
    rebuild, errors = _run(
        lambda: transport.reconfigure_connect(("tcp/127.0.0.1:7447",)), name="redial"
    )
    rebuild.join(10)
    elapsed = time.monotonic() - started
    assert not rebuild.is_alive() and not errors
    assert elapsed < 1.5, f"the rebuild waited {elapsed:.1f}s on a liveliness callback"
    deadline = time.monotonic() + 5
    while not any(put for put in sessions[-1].puts) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert all(outcome is None for outcome in announced), announced
    assert ("org/fs/announce/self", b"a") in sessions[-1].puts
