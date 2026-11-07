"""ZenohTransport.reconfigure_connect: dial a new endpoint set without a restart.

zenoh-python 1.9 has no live setter for a session's connect endpoints, so the
transport rebuilds only its session and replays every live declaration onto
it (forwarding defaults plan §C, step 2; the brief interruption is approved).
These run against real loopback sessions: the claim is that TRAFFIC reaches a
peer the session did not dial before, through the declarations the caller
already holds -- not that a new tuple was stored.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable, Iterator

import pytest

from hyprial.daemon.impl.transport.keys import KeySpace
from hyprial.daemon.impl.transport.zenoh.presence import LivelinessDirectory
from hyprial.daemon.impl.transport.zenoh.config import ZenohConfig
from hyprial.daemon.impl.transport.zenoh import transport as transport_module
from hyprial.daemon.impl.transport.zenoh.transport import ZenohTransport


def _endpoint() -> str:
    # zenoh will not listen on port 0; hand it a real free port.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"tcp/127.0.0.1:{probe.getsockname()[1]}"


def _eventually(check: Callable[[], bool], *, act: Callable[[], None] | None = None, seconds: float = 8.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if act is not None:
            act()
        if check():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def pair() -> Iterator[tuple[ZenohTransport, ZenohTransport, str]]:
    """A (dials nobody) and B (listening on b): not connected to each other."""

    a_listen, b_listen = _endpoint(), _endpoint()
    a = ZenohTransport(ZenohConfig(listen=(a_listen,)))
    b = ZenohTransport(ZenohConfig(listen=(b_listen,)))
    try:
        yield a, b, b_listen
    finally:
        a.close()
        b.close()


def test_a_rebuilt_session_reaches_a_new_peer_through_existing_subscriptions(pair) -> None:
    a, b, b_endpoint = pair
    received: list[bytes] = []
    subscription = a.subscribe("reconf/**", lambda sample: received.append(sample.payload))

    # Not connected yet: a put from B cannot arrive.
    b.put("reconf/x", b"before")
    time.sleep(0.5)
    assert received == []

    a.reconfigure_connect((b_endpoint,))

    assert _eventually(lambda: bool(received), act=lambda: b.put("reconf/x", b"after"))
    assert set(received) == {b"after"}
    subscription.close()


def test_orgfs_queryable_answers_after_responder_session_rebuild(pair) -> None:
    responder, requester, requester_endpoint = pair
    key = KeySpace().orgfs_sync("space-large", "node-responder")
    seen: list[bytes | None] = []

    def answer(selector: str, payload: bytes | None):
        seen.append(payload)
        return [(selector, b"sync-page")]

    registration = responder.declare_query_handler(key, answer)
    responder.reconfigure_connect((requester_endpoint,))
    try:
        assert _eventually(
            lambda: bool(requester.query(key, b"sync-request", timeout=0.2))
        )
        replies = requester.query(key, b"sync-request-2", timeout=1.0)
        assert [reply.payload for reply in replies] == [b"sync-page"]
        assert seen[-1] == b"sync-request-2"
    finally:
        registration.close()


def test_a_registration_closed_before_the_rebuild_is_not_replayed(pair) -> None:
    a, b, b_endpoint = pair
    closed: list[bytes] = []
    live: list[bytes] = []
    a.subscribe("reconf/**", lambda sample: closed.append(sample.payload)).close()
    a.subscribe("reconf/**", lambda sample: live.append(sample.payload))

    # The transport forgot the closed one, so a long-lived daemon's list of
    # declarations does not grow with every handle it ever closed.
    assert len(a._registrations) == 1

    a.reconfigure_connect((b_endpoint,))

    assert _eventually(lambda: bool(live), act=lambda: b.put("reconf/x", b"m"))
    assert closed == []


def test_presence_is_rebuilt_not_carried_over(pair) -> None:
    """Departed peers must not stay "online" across a rebuild, and present ones
    must come back (the liveliness observers replay with history)."""

    a, b, b_endpoint = pair
    keys = KeySpace()
    token_key = f"{keys.prefix}/liveliness/actor/{keys.encode_identity('agent:o:n:peer')}"
    token = b.declare_liveliness(token_key)
    directory = LivelinessDirectory(a, keys)
    try:
        a.reconfigure_connect((b_endpoint,))
        assert _eventually(lambda: directory.actor_online("agent:o:n:peer"))

        a.reconfigure_connect(())  # dial nobody: the peer is gone from A's view
        assert directory.actor_online("agent:o:n:peer") is False

        a.reconfigure_connect((b_endpoint,))
        assert _eventually(lambda: directory.actor_online("agent:o:n:peer"))
    finally:
        directory.close()
        token.close()


def test_an_invalid_endpoint_is_refused_before_the_session_is_touched(pair) -> None:
    a, b, b_endpoint = pair
    a.reconfigure_connect((b_endpoint,))
    received: list[bytes] = []
    a.subscribe("reconf/**", lambda sample: received.append(sample.payload))

    with pytest.raises(ValueError):
        a.reconfigure_connect(("not-an-endpoint",))

    # The old session and its declarations still carry traffic.
    assert _eventually(lambda: bool(received), act=lambda: b.put("reconf/x", b"still"))


class _FakeSession:
    """A session whose close can raise after it has closed (zenoh 1.9)."""

    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.closed = False
        self.close_error = close_error
        self.declared: list[str] = []
        self.puts: list[tuple[str, bytes]] = []
        self.put_error: Exception | None = None
        self.declare_error: Exception | None = None

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

    def liveliness(self):
        return self

    def put(self, key, payload, **_kwargs):
        if self.put_error is not None:
            self.put_error, error = None, self.put_error
            raise error
        self.puts.append((key, payload))

    def declare_token(self, key):
        if self.declare_error is not None:
            self.declare_error, error = None, self.declare_error
            raise error
        self.declared.append(key)
        return _FakeToken()

    def zid(self) -> str:
        return "fake"


class _FakeToken:
    def close(self) -> None:
        return None


def test_a_close_that_raises_after_closing_is_still_replaced(monkeypatch) -> None:
    """zenoh 1.9 close can close the session and then raise; the rebuild must
    still open a new session instead of keeping the dead one."""

    a = ZenohTransport(ZenohConfig(listen=(_endpoint(),)))
    original = a._session
    original.close()
    fake_old = _FakeSession(close_error=RuntimeError("session closed"))
    opened = _FakeSession()
    a._session = fake_old
    monkeypatch.setattr(transport_module.zenoh, "open", lambda _config: opened)
    try:
        a.reconfigure_connect(())
        assert a._session is opened
        a.declare_liveliness("probe/after-close")
        assert opened.declared == ["probe/after-close"]
    finally:
        a.close()


def test_both_opens_failing_never_keeps_the_closed_session(monkeypatch) -> None:
    """If candidate and fallback opens both fail, the transport must not keep a
    closed session as its authority; it degrades and retries with a bound."""

    a = ZenohTransport(ZenohConfig(listen=(_endpoint(),)))
    original = a._session
    original.close()
    fake_old = _FakeSession(close_error=RuntimeError("session closed"))
    a._session = fake_old

    def fail(_config):
        raise RuntimeError("open failed")

    monkeypatch.setattr(transport_module.zenoh, "open", fail)
    try:
        with pytest.raises(RuntimeError, match="open failed"):
            a.reconfigure_connect(())
        assert a._session is not fake_old
        assert a._session is None
        with pytest.raises(RuntimeError, match="degraded|unavailable"):
            a.declare_liveliness("probe/degraded")
    finally:
        a.close()


def test_a_failed_replay_still_clears_rebuilding(pair, monkeypatch) -> None:
    """Replay runs inside the rebuild gate; an exception there must not leave
    every caller waiting forever."""

    a, b, b_endpoint = pair
    a.reconfigure_connect((b_endpoint,))

    def boom() -> None:
        raise RuntimeError("replay boom")

    monkeypatch.setattr(a, "_replay", boom)
    with pytest.raises(RuntimeError, match="replay boom"):
        a.reconfigure_connect(())
    monkeypatch.undo()
    assert a._rebuilding is None
    a.declare_liveliness("probe/after-boom")


def test_a_publish_on_a_closed_session_reopens_and_retries_once(monkeypatch) -> None:
    a = ZenohTransport(ZenohConfig(listen=(_endpoint(),)))
    original = a._session
    original.close()
    fake_old = _FakeSession()
    fake_old.put_error = RuntimeError("ZError: session closed")
    opened = _FakeSession()
    a._session = fake_old
    monkeypatch.setattr(transport_module.zenoh, "open", lambda _config: opened)
    try:
        a.put("probe/receipt", b"ack")
        assert opened.puts == [("probe/receipt", b"ack")]
    finally:
        a.close()


class _AlwaysClosedSession(_FakeSession):
    def put(self, key, payload, **_kwargs):
        raise RuntimeError("ZError: session closed")


def test_concurrent_publishes_reopen_the_dead_session_once(monkeypatch) -> None:
    import threading

    a = ZenohTransport(ZenohConfig(listen=(_endpoint(),)))
    original = a._session
    original.close()
    dead = _AlwaysClosedSession()
    opened = _FakeSession()
    a._session = dead
    opens = 0

    def fake_open(_config):
        nonlocal opens
        opens += 1
        return opened

    monkeypatch.setattr(transport_module.zenoh, "open", fake_open)
    threads = 8
    barrier = threading.Barrier(threads)
    errors: list[BaseException] = []

    def publish(index: int) -> None:
        barrier.wait()
        try:
            a.put(f"probe/{index}", b"ack")
        except BaseException as error:  # noqa: BLE001 - surfaced below
            errors.append(error)

    workers = [threading.Thread(target=publish, args=(i,)) for i in range(threads)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
        assert not errors
        assert opens == 1
        assert len(opened.puts) == threads
    finally:
        a.close()


def test_a_declare_on_a_closed_session_reopens_and_retries_once(monkeypatch) -> None:
    a = ZenohTransport(ZenohConfig(listen=(_endpoint(),)))
    original = a._session
    original.close()
    fake_old = _FakeSession()
    fake_old.declare_error = RuntimeError("ZError: session closed")
    opened = _FakeSession()
    a._session = fake_old
    monkeypatch.setattr(transport_module.zenoh, "open", lambda _config: opened)
    try:
        registration = a.declare_liveliness("probe/live")
        assert opened.declared == ["probe/live"]
        registration.close()
    finally:
        a.close()


def test_a_degraded_session_recovers_without_a_terminal_attempt(monkeypatch) -> None:
    import time

    a = ZenohTransport(ZenohConfig(listen=(_endpoint(),)))
    original = a._session
    original.close()
    a._session = _AlwaysClosedSession()
    a._DEGRADED_RECOVERY_INITIAL_SECONDS = 0.01
    a._DEGRADED_RECOVERY_MAX_SECONDS = 0.02
    opened = _FakeSession()
    opens = 0
    events: list[str] = []
    original_emit = a._emit_diagnostic

    def record(event, message, *args, **fields):
        events.append(event)
        original_emit(event, message, *args, **fields)

    a._emit_diagnostic = record  # type: ignore[method-assign]

    def fake_open(_config):
        nonlocal opens
        opens += 1
        if opens <= 2:
            raise RuntimeError("open failed")
        return opened

    monkeypatch.setattr(transport_module.zenoh, "open", fake_open)

    def replay_boom() -> None:
        raise RuntimeError("replay boom")

    a._replay = replay_boom  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="open failed"):
            a.reconfigure_connect(())
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and a.session_health()["state"] != "open":
            time.sleep(0.01)
        assert a.session_health()["state"] == "open"
        assert a._session is opened
        assert a.session_health()["recoveryAttempts"] == 0
        assert "zenoh.session.replay_failed" in events
    finally:
        a.close()


def test_recovery_and_redial_never_install_two_sessions(monkeypatch) -> None:
    import threading
    import time

    a = ZenohTransport(ZenohConfig(listen=(_endpoint(),)))
    original = a._session
    original.close()
    a._session = _AlwaysClosedSession()
    a._DEGRADED_RECOVERY_INITIAL_SECONDS = 0.01
    a._DEGRADED_RECOVERY_MAX_SECONDS = 0.02
    recovery_open_started = threading.Event()
    release = threading.Event()
    sessions: list[_FakeSession] = []
    failures = {"n": 0}

    def fake_open(_config):
        if failures["n"] < 2:
            failures["n"] += 1
            raise RuntimeError("open failed")
        session = _FakeSession()
        sessions.append(session)
        if len(sessions) == 1:
            recovery_open_started.set()
            assert release.wait(2)
        return session

    monkeypatch.setattr(transport_module.zenoh, "open", fake_open)
    errors: list[BaseException] = []
    try:
        with pytest.raises(RuntimeError, match="open failed"):
            a.reconfigure_connect(())
        assert recovery_open_started.wait(2)

        def redial() -> None:
            try:
                a.reconfigure_connect(())
            except BaseException as error:  # noqa: BLE001 - surfaced below
                errors.append(error)

        worker = threading.Thread(target=redial)
        worker.start()
        time.sleep(0.05)  # the redial must wait on the reopen lock
        release.set()
        worker.join(3)
        assert not errors
        assert len(sessions) == 2
        recovery, redialed = sessions
        assert recovery.closed is True
        assert a._session is redialed
        assert a._rebuilding is None
    finally:
        release.set()
        a.close()


def test_a_unixsock_slot_locator_is_a_valid_reconfigure_target(pair, tmp_path) -> None:
    """The slot-pool locator shape (``unixsock-stream//abs/path``) must pass
    the pre-flight check: slot growth redials through here, and the pool's
    whole point is that the target may not be listening yet (hold mode)."""

    a, b, b_endpoint = pair
    slot = f"unixsock-stream/{tmp_path / 'slot-00'}"
    a.reconfigure_connect((b_endpoint, slot))
    assert a.config.connect == (b_endpoint, slot)

    # The malformed cousins are still refused.
    for bad in ("unixsock-stream/relative/path", "unixsock-stream/", "unixsock-stream"):
        with pytest.raises(ValueError):
            a.reconfigure_connect((bad,))
