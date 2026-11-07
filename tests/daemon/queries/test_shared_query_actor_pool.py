"""Shared query pool custody through close and session rebuild."""

from __future__ import annotations

import socket
import logging
import threading
import time
from pathlib import Path

import pytest

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport
from hyprial.daemon.impl.transport.session_actor import TransportSessionAuthority
from hyprial.daemon.impl.transport.zenoh.dispatchers import _QueryDispatcher


def _endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return f"tcp/127.0.0.1:{probe.getsockname()[1]}"


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _pair() -> tuple[ZenohTransport, ZenohTransport]:
    endpoint = _endpoint()
    owner = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    observer = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    time.sleep(0.3)
    return owner, observer


def test_composed_transport_close_keeps_held_query_until_retry() -> None:
    native, observer = _pair()
    authority = TransportSessionAuthority(native)
    key = "hyprial/test/query-custody/close"
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    dropped = threading.Event()

    class Query:
        selector = key
        payload = b"request"

        def __init__(self) -> None:
            self.replies: list[tuple[str, bytes]] = []
            self.drop_count = 0
            self.events: list[str] = []

        def reply(self, reply_key: str, payload: bytes, **_options: object) -> None:
            self.replies.append((reply_key, payload))
            self.events.append("reply")

        def drop(self) -> None:
            self.drop_count += 1
            self.events.append("drop")
            dropped.set()

    def handler(selector: str, _payload: bytes | None):
        calls.append(selector)
        entered.set()
        assert release.wait(5)
        yield key, b"settled"

    authority.declare_query_handler(key, handler)
    query_owner = next(iter(native._query_registrations))
    query = Query()
    try:
        assert query_owner.admit(query) is AdmissionResult.ACCEPTED
        assert entered.wait(2)
        with pytest.raises(TimeoutError, match="remains accepted"):
            authority.close(0.05)
        assert native._native_closed is False
        assert len(calls) == 1
        release.set()
        assert dropped.wait(3)
        # CloseSession advances the control actor generation before this
        # generator yields, so its answer_many wrapper fences the late reply.
        assert query.replies == []
        assert query.events == ["drop"]
        assert query.drop_count == 1
        assert query_owner.projection().failed == 0
        authority.close(3)
        assert native._native_closed is True
        assert len(calls) == 1
    finally:
        release.set()
        try:
            authority.close(3)
        except Exception:
            pass
        observer.close()


def test_direct_registration_pool_replies_before_native_drop() -> None:
    native, observer = _pair()
    key = "hyprial/test/query-custody/direct"
    finished = threading.Event()

    class Query:
        selector = key
        payload = b"request"

        def __init__(self) -> None:
            self.events: list[tuple[str, object]] = []

        def reply(self, reply_key: str, payload: bytes, **_options: object) -> None:
            self.events.append(("reply", (reply_key, payload)))

        def drop(self) -> None:
            self.events.append(("drop", None))
            finished.set()

    registration = native.declare_query_handler(
        key, lambda _selector, _payload: ((key, b"settled"),)
    )
    owner = next(iter(native._query_registrations))
    query = Query()
    try:
        assert owner.admit(query) is AdmissionResult.ACCEPTED
        assert finished.wait(2)
        assert query.events == [
            ("reply", (key, b"settled")), ("drop", None)
        ]
        assert owner.projection().failed == 0
        deadline = time.monotonic() + 2
        while owner.projection().outstanding and time.monotonic() < deadline:
            time.sleep(0.01)
        assert owner.projection().outstanding == 0
    finally:
        registration.close()
        observer.close()
        native.close()


def test_failed_native_query_close_retries_same_accepted_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native, observer = _pair()
    authority = TransportSessionAuthority(native)
    key = "hyprial/test/query-custody/failed-close"
    entered = threading.Event()
    release = threading.Event()
    dropped = threading.Event()
    calls: list[str] = []

    class Query:
        selector = key
        payload = b"request"

        def __init__(self) -> None:
            self.drop_count = 0

        def reply(self, _key: str, _payload: bytes, **_options: object) -> None:
            return None

        def drop(self) -> None:
            self.drop_count += 1
            dropped.set()

    def handler(selector: str, _payload: bytes | None):
        calls.append(selector)
        entered.set()
        assert release.wait(5)
        yield key, b"settled"

    authority.declare_query_handler(key, handler)
    owner = next(iter(native._query_registrations))
    real_close = owner.close
    attempts = 0

    def refuse_once(timeout: float = 1.0) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        return real_close(timeout)

    monkeypatch.setattr(owner, "close", refuse_once)
    query = Query()
    try:
        assert owner.admit(query) is AdmissionResult.ACCEPTED
        assert entered.wait(2)
        with pytest.raises(TimeoutError, match="native query effects did not drain"):
            authority.close(2)
        assert native._native_closed is False
        assert len(calls) == 1
        release.set()
        assert dropped.wait(3)
        authority.close(3)
        assert attempts == 2
        assert native._native_closed is True
        assert len(calls) == 1
        assert query.drop_count == 1
    finally:
        release.set()
        try:
            authority.close(3)
        except Exception:
            pass
        observer.close()


def test_rebuild_waits_for_query_handler_put_before_replacing_session() -> None:
    owner, observer = _pair()
    key = "hyprial/test/query-custody/rebuild"
    sample_key = key + "/sample"
    entered = threading.Event()
    permit_put = threading.Event()
    got_sample = threading.Event()
    query_results: list[bytes] = []
    errors: list[BaseException] = []
    subscriber = observer.subscribe(sample_key, lambda _sample: got_sample.set())

    def handler(_selector: str, _payload: bytes | None):
        entered.set()
        assert permit_put.wait(5)
        owner.put(sample_key, b"from-held-handler")
        yield key, b"done"

    registration = owner.declare_query_handler(key, handler)

    def request() -> None:
        try:
            query_results.extend(item.payload for item in observer.query(key, b"q", timeout=3))
        except BaseException as error:
            errors.append(error)

    def rebuild() -> None:
        try:
            owner.reconfigure_connect(())
        except BaseException as error:
            errors.append(error)

    waiter = threading.Thread(target=request, daemon=True)
    rebuilder = threading.Thread(target=rebuild, daemon=True)
    try:
        time.sleep(0.3)
        waiter.start()
        assert entered.wait(2)
        rebuilder.start()
        time.sleep(0.05)
        assert rebuilder.is_alive()
        assert owner._rebuilding is None
        permit_put.set()
        assert got_sample.wait(2), "accepted handler could not put on old session"
        waiter.join(3)
        rebuilder.join(3)
        assert not waiter.is_alive() and not rebuilder.is_alive()
        assert errors == []
        assert query_results == [b"done"]
    finally:
        permit_put.set()
        waiter.join(3)
        rebuilder.join(3)
        registration.close()
        subscriber.close()
        observer.close()
        owner.close()


def test_rebuild_timeout_preserves_old_session_and_resumes_query_admission() -> None:
    owner, observer = _pair()
    key = "hyprial/test/query-custody/rebuild-timeout"
    entered = threading.Event()
    release = threading.Event()
    old_session = owner._session

    def handler(_selector: str, _payload: bytes | None):
        entered.set()
        assert release.wait(8)
        yield key, b"settled"

    registration = owner.declare_query_handler(key, handler)
    waiter = threading.Thread(target=lambda: observer.query(key, b"q", timeout=8), daemon=True)
    try:
        time.sleep(0.3)
        waiter.start()
        assert entered.wait(2)
        with pytest.raises(TimeoutError, match="did not drain before rebuild"):
            owner.reconfigure_connect(())
        assert owner._session is old_session
        assert owner._rebuilding is None
        assert owner._closed is False
        assert owner._query_dispatcher._paused is False
    finally:
        release.set()
        waiter.join(3)
        registration.close()
        observer.close()
        owner.close()


def test_failed_native_drop_retains_exact_cell_until_explicit_retry() -> None:
    native, observer = _pair()
    key = "hyprial/test/query-custody/drop-retry"
    handled = threading.Event()
    calls = 0

    class Query:
        selector = key
        payload = b"request"

        def __init__(self) -> None:
            self.drop_count = 0
            self.replies: list[bytes] = []

        def reply(self, _key: str, payload: bytes, **_options: object) -> None:
            self.replies.append(payload)

        def drop(self) -> None:
            self.drop_count += 1
            if self.drop_count == 1:
                raise RuntimeError("synthetic first drop failure")

    def handler(_selector: str, _payload: bytes | None):
        nonlocal calls
        calls += 1
        handled.set()
        yield key, b"once"

    registration = native.declare_query_handler(key, handler)
    owner = next(iter(native._query_registrations))
    query = Query()
    try:
        assert owner.admit(query) is AdmissionResult.ACCEPTED
        assert handled.wait(2)
        deadline = time.monotonic() + 2
        while not native._query_dispatcher._cleanup_pending and time.monotonic() < deadline:
            time.sleep(0.01)
        assert native._query_dispatcher._outstanding == 1
        assert owner.projection().outstanding == 1
        assert query.replies == [b"once"]
        assert query.drop_count == 1
        assert native._native_closed is False
        registration.close(0.01)
        assert query.drop_count == 2
        assert owner.projection().outstanding == 0
        assert native._query_dispatcher._outstanding == 0
        assert calls == 1
    finally:
        try:
            registration.close()
        except Exception:
            pass
        observer.close()
        native.close()


def test_failed_retirement_retries_without_repeating_successful_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native, observer = _pair()
    key = "hyprial/test/query-custody/retire-retry"
    handled = threading.Event()
    calls = 0

    class Query:
        selector = key
        payload = b"request"

        def __init__(self) -> None:
            self.drop_count = 0

        def reply(self, _key: str, _payload: bytes, **_options: object) -> None:
            return None

        def drop(self) -> None:
            self.drop_count += 1

    def handler(_selector: str, _payload: bytes | None):
        nonlocal calls
        calls += 1
        handled.set()
        yield key, b"once"

    registration = native.declare_query_handler(key, handler)
    owner = next(iter(native._query_registrations))
    real_retire = owner._retire
    retire_attempts = 0

    def retire_once(token: str) -> None:
        nonlocal retire_attempts
        retire_attempts += 1
        if retire_attempts == 1:
            raise RuntimeError("synthetic retirement failure")
        real_retire(token)

    monkeypatch.setattr(owner, "_retire", retire_once)
    query = Query()
    try:
        assert owner.admit(query) is AdmissionResult.ACCEPTED
        assert handled.wait(2)
        deadline = time.monotonic() + 2
        while not native._query_dispatcher._cleanup_pending and time.monotonic() < deadline:
            time.sleep(0.01)
        assert query.drop_count == 1
        assert native._query_dispatcher._outstanding == 1
        assert owner.projection().outstanding == 1
        registration.close(0.01)
        assert retire_attempts == 2
        assert query.drop_count == 1
        assert owner.projection().outstanding == 0
        assert native._query_dispatcher._outstanding == 0
        assert calls == 1
    finally:
        try:
            registration.close()
        except Exception:
            pass
        observer.close()
        native.close()


def test_rejected_query_drop_failure_is_observable_without_handler_admission(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatcher = _QueryDispatcher(capacity=1, workers=1)
    entered = threading.Event()
    release = threading.Event()
    handled: list[object] = []

    class Query:
        def __init__(self, *, fail_drop: bool = False) -> None:
            self.fail_drop = fail_drop
            self.drops = 0

        def drop(self) -> None:
            self.drops += 1
            if self.fail_drop:
                raise RuntimeError("synthetic refused-query drop failure")

    def handler(query: Query) -> None:
        handled.append(query)
        entered.set()
        assert release.wait(3)

    deliver = dispatcher.wrap("query/refusal", handler)
    active = Query()
    queued = Query()
    refused = Query(fail_drop=True)
    try:
        deliver(active)
        assert entered.wait(1)
        deliver(queued)
        with caplog.at_level(logging.ERROR, logger="hyprial.transport.zenoh"):
            deliver(refused)
        assert refused.drops == 1
        assert refused not in handled
        assert dispatcher._outstanding == 2
        assert "zenoh.queryable.drop_failed" in caplog.text
    finally:
        release.set()
        assert dispatcher.stop(2)
    assert active.drops == 1
    assert queued.drops == 1
    assert refused not in handled


def test_concurrent_cleanup_retries_do_not_repeat_native_drop() -> None:
    dispatcher = _QueryDispatcher(capacity=1, workers=1)
    first_failed = threading.Event()
    second_entered = threading.Event()
    release_second = threading.Event()
    calls: list[str] = []
    outcomes: list[bool] = []

    class Query:
        def __init__(self) -> None:
            self.drops = 0

        def drop(self) -> None:
            self.drops += 1
            if self.drops == 1:
                first_failed.set()
                raise RuntimeError("first native drop failed")
            if self.drops == 2:
                second_entered.set()
                assert release_second.wait(3)

    query = Query()
    deliver = dispatcher.wrap("query/retry-serial", lambda _query: calls.append("handler"))
    try:
        deliver(query)
        assert first_failed.wait(2)
        deadline = time.monotonic() + 2
        while not dispatcher._cleanup_pending and time.monotonic() < deadline:
            time.sleep(0.01)
        assert dispatcher._outstanding == 1
        first = threading.Thread(target=lambda: outcomes.append(dispatcher.retry_cleanup()))
        second = threading.Thread(target=lambda: outcomes.append(dispatcher.retry_cleanup()))
        first.start()
        assert second_entered.wait(2)
        second.start()
        time.sleep(0.05)
        assert second.is_alive(), "second retry ran native cleanup concurrently"
        release_second.set()
        first.join(2)
        second.join(2)
        assert not first.is_alive() and not second.is_alive()
        assert outcomes == [True, True]
        assert query.drops == 2
        assert calls == ["handler"]
        assert dispatcher._outstanding == 0
    finally:
        release_second.set()
        assert dispatcher.stop(2)


def test_close_waits_for_native_query_declaration_before_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native, observer = _pair()
    key = "hyprial/test/query-custody/register-race"
    registered = threading.Event()
    release = threading.Event()
    original_register = native._register
    errors: list[BaseException] = []
    created: list[object] = []

    def delayed_register(declare):
        registration = original_register(declare)
        registered.set()
        assert release.wait(3)
        return registration

    monkeypatch.setattr(native, "_register", delayed_register)

    def declare() -> None:
        try:
            created.append(native.declare_queryable(key, lambda _selector: b"ok"))
        except BaseException as error:
            errors.append(error)

    def close() -> None:
        try:
            native.close()
        except BaseException as error:
            errors.append(error)

    declaring = threading.Thread(target=declare, daemon=True)
    closing = threading.Thread(target=close, daemon=True)
    try:
        declaring.start()
        assert registered.wait(2)
        closing.start()
        time.sleep(0.05)
        assert closing.is_alive()
        assert native._native_closed is False
        release.set()
        declaring.join(3)
        closing.join(3)
        assert not declaring.is_alive() and not closing.is_alive()
        assert errors == []
        assert len(created) == 1
        assert native._native_closed is True
        assert native.query_status() == ()
    finally:
        release.set()
        declaring.join(3)
        closing.join(3)
        observer.close()
        try:
            native.close()
        except Exception:
            pass
