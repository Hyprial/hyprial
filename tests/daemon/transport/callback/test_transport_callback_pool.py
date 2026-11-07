"""Production-shaped route callback admission and bounded worker custody."""
from __future__ import annotations

from types import SimpleNamespace
import threading
import time

import pytest

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.transport.api import TransportSample

from hyprial.daemon.impl.inbox.links.zenoh_link import ZenohInboxEndpoint
from hyprial.daemon.impl.transport.session_actor import TransportSessionAuthority


class _Registration:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Native:
    config = SimpleNamespace(connect=())

    def __init__(self) -> None:
        self.registrations: list[_Registration] = []
        self.callbacks: dict[str, object] = {}
        self.closed = False

    def _register(self) -> _Registration:
        registration = _Registration()
        self.registrations.append(registration)
        return registration

    def declare_liveliness(self, _key: str) -> _Registration:
        return self._register()

    def subscribe(self, key: str, callback: object) -> _Registration:
        self.callbacks[key] = callback
        return self._register()

    def reconfigure_connect(self, endpoints) -> None:
        self.config = SimpleNamespace(connect=tuple(endpoints))

    def close(self) -> None:
        self.closed = True


def test_320_production_route_pairs_fit_fixed_callback_workers() -> None:
    native = _Native()
    owner = TransportSessionAuthority(native)
    startup = []
    routes: list[tuple[_Registration | object, ZenohInboxEndpoint]] = []
    try:
        # Model 15 occupied startup cells, inferred from the live failure's
        # threshold. This is not a directly measured native registration count.
        for index in range(15):
            startup.append(owner.subscribe(f"startup/{index}", lambda _sample: None))
        for index in range(320):
            actor = f"agent:owner:node:local-{index:03d}"
            token = owner.declare_liveliness(f"liveliness/{actor}")
            endpoint = ZenohInboxEndpoint(
                owner, object(), actor, declare_receipts=False
            )
            routes.append((token, endpoint))
        assert owner.projection().registrations == 15 + 320 * 2
        snapshot = owner.callback_pool_snapshot()
        assert (snapshot.registrations, snapshot.outstanding, snapshot.workers) == (335, 0, 4)
        assert len(owner._callback_pool._lane._threads) == 4
        assert all(thread.is_alive() for thread in owner._callback_pool._lane._threads)
        with owner._runtime._guardian._lock:
            # Only the transport control actor exists: route growth does not
            # quietly reintroduce one backend actor/thread per callback cell.
            assert len(owner._runtime._guardian._children) == 1
        assert len(native.registrations) == 15 + 320 * 2
    finally:
        for token, endpoint in reversed(routes):
            endpoint.close()
            token.close()
        for registration in reversed(startup):
            registration.close()
        owner.close()
    assert native.closed


def _emit(native: _Native, key: str, payload: bytes = b"x") -> None:
    native.callbacks[key](TransportSample(key, payload))  # type: ignore[operator]


def test_supported_route_history_fits_one_callback_registration() -> None:
    class HistoryNative(_Native):
        def observe_liveliness(self, key, callback, *, history):
            registration = self.subscribe(key, callback)
            assert history
            for index in range(320):
                callback(TransportSample(f"actor/agent:owner:node:local-{index}", b""))
            callback(TransportSample("actor/node", b""))
            return registration

    native = HistoryNative()
    owner = TransportSessionAuthority(native)
    entered, release = threading.Event(), threading.Event()
    seen = []
    registration = None
    other_seen = threading.Event()
    other = owner.subscribe("other", lambda _sample: other_seen.set())

    def hold_first(sample):
        if not seen:
            entered.set()
            assert release.wait(5)
        seen.append(sample.key)

    try:
        registration = owner.observe_liveliness("actor/*", hold_first, history=True)
        assert entered.wait(1)
        snapshot = owner.callback_pool_snapshot()
        assert snapshot.workers == 4
        assert snapshot.outstanding == 321
        assert owner.projection().callbacks_complete is True
        assert owner.projection().rejected == 0
        _emit(native, "other")
        assert other_seen.wait(1), "a held history callback stalled another registration"
        release.set()
        _wait_for(lambda: len(seen) == 321)
        assert seen[-1] == "actor/node"
        assert len(set(seen)) == 321
        assert owner.projection().generation == 1
    finally:
        release.set()
        if registration is not None:
            registration.close()
        other.close()
        owner.close()


def test_history_above_default_share_is_explicitly_incomplete(monkeypatch) -> None:
    native = _Native()
    native.observe_liveliness = lambda key, callback, **_kw: native.subscribe(key, callback)
    owner = TransportSessionAuthority(native)
    entered, release = threading.Event(), threading.Event()
    recovery_requested = []
    # Hold recovery scheduling so the rejection projection can be inspected
    # before a new generation replaces it. Separate tests run actual recovery.
    monkeypatch.setattr(owner, "_schedule_recovery", lambda: recovery_requested.append(True))

    def held(_sample):
        entered.set()
        assert release.wait(5)

    registration = owner.observe_liveliness("actor/*", held, history=True)
    try:
        callback = native.callbacks["actor/*"]
        callback(TransportSample("actor/local-0", b""))
        assert entered.wait(1)
        for index in range(1, 512):
            callback(TransportSample(f"actor/local-{index}", b""))
        callback(TransportSample("actor/host", b""))
        projection = owner.projection()
        assert projection.callbacks_complete is False
        assert projection.rejected == 1
        assert owner.callback_pool_snapshot().outstanding == 512
        assert owner.callback_pool_snapshot().workers == 4
        assert recovery_requested
    finally:
        release.set()
        registration.close()
        owner.close()


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "accepted callback never settled"
        time.sleep(0.01)


def test_held_callback_retains_native_session_and_registration_credit() -> None:
    native = _Native()
    owner = TransportSessionAuthority(
        native, callback_registration_capacity=1, callback_outstanding_capacity=2
    )
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def held(_sample: TransportSample) -> None:
        calls.append("held")
        entered.set()
        assert release.wait(5)

    registration = owner.subscribe("sample/held", held)
    try:
        _emit(native, "sample/held")
        assert entered.wait(2)
        registration.close()
        snapshot = owner.callback_pool_snapshot()
        assert (snapshot.registrations, snapshot.retired, snapshot.outstanding) == (1, 1, 1)
        with pytest.raises(RuntimeError, match="callback registration capacity exhausted"):
            owner.subscribe("sample/overflow", lambda _sample: None)
        with pytest.raises(TimeoutError, match="callbacks still draining"):
            owner.close(timeout=0.03)
        assert not native.closed
        assert calls == ["held"]
        release.set()
        owner.close(timeout=5.0)
        assert native.closed
        assert calls == ["held"]
        assert owner.callback_pool_snapshot().outstanding == 0
    finally:
        release.set()


def test_same_token_fifo_allows_other_token_progress() -> None:
    native = _Native()
    owner = TransportSessionAuthority(native, callback_registration_capacity=2)
    entered = threading.Event()
    release = threading.Event()
    other_done = threading.Event()
    seen: list[bytes] = []

    def first(sample: TransportSample) -> None:
        if sample.payload == b"a":
            entered.set()
            assert release.wait(5)
        seen.append(sample.payload)

    one = owner.subscribe("sample/one", first)
    two = owner.subscribe("sample/two", lambda _sample: other_done.set())
    try:
        _emit(native, "sample/one", b"a")
        assert entered.wait(2)
        _emit(native, "sample/one", b"b")
        _emit(native, "sample/two")
        assert other_done.wait(2)
        assert seen == []
        release.set()
        _wait_for(lambda: seen == [b"a", b"b"])
        assert owner.callback_pool_snapshot().workers == 4
    finally:
        release.set()
        two.close()
        one.close()
        owner.close()


def test_per_token_and_global_admission_credit_is_exact() -> None:
    native = _Native()
    owner = TransportSessionAuthority(
        native, callback_registration_capacity=2,
        callback_capacity=2, callback_outstanding_capacity=3,
    )
    entered = threading.Event()
    release = threading.Event()

    def held(_sample: TransportSample) -> None:
        entered.set()
        assert release.wait(5)

    one = owner.subscribe("sample/one", held)
    two = owner.subscribe("sample/two", held)
    try:
        _emit(native, "sample/one")
        assert entered.wait(2)
        _emit(native, "sample/one")
        token_one = next(token for token, callback in owner._callbacks.items() if callback is held)
        sample = TransportSample("sample/one", b"x", generation=owner.projection().generation)
        from hyprial.daemon.impl.transport.session_actor import CallbackObserved
        assert owner._callback_pool.admit(
            token_one, CallbackObserved(owner.projection().generation, sample)
        ) is AdmissionResult.OVERLOADED
        _emit(native, "sample/two")
        _wait_for(lambda: owner.callback_pool_snapshot().outstanding == 3)
        token_two = next(token for token, callback in owner._callbacks.items() if callback is held and token != token_one)
        assert owner._callback_pool.admit(
            token_two, CallbackObserved(owner.projection().generation, sample)
        ) is AdmissionResult.OVERLOADED
        assert owner.callback_pool_snapshot().outstanding == 3
    finally:
        release.set()
        two.close()
        one.close()
        owner.close()


def test_callback_registration_capacity_releases_only_after_retirement() -> None:
    native = _Native()
    owner = TransportSessionAuthority(native, callback_registration_capacity=2)
    one = owner.subscribe("sample/one", lambda _sample: None)
    two = owner.subscribe("sample/two", lambda _sample: None)
    try:
        assert owner.callback_pool_snapshot().registrations == 2
        with pytest.raises(RuntimeError, match="callback registration capacity exhausted"):
            owner.subscribe("sample/three", lambda _sample: None)
        one.close()
        assert owner.callback_pool_snapshot().registrations == 1
        three = owner.subscribe("sample/three", lambda _sample: None)
        three.close()
    finally:
        two.close()
        owner.close()


def test_native_callback_overflow_invalidates_history_projection() -> None:
    native = _Native()
    owner = TransportSessionAuthority(
        native, callback_capacity=1, callback_outstanding_capacity=2
    )
    entered = threading.Event()
    release = threading.Event()

    def held(_sample: TransportSample) -> None:
        entered.set()
        assert release.wait(5)

    registration = owner.subscribe("sample/held", held)
    try:
        _emit(native, "sample/held")
        assert entered.wait(2)
        _emit(native, "sample/held")
        projection = owner.projection()
        assert projection.callbacks_complete is False
        assert projection.rejected >= 1
        assert owner.callback_pool_snapshot().outstanding == 1
    finally:
        release.set()
        registration.close()
        owner.close()


def test_rebuild_fences_old_generation_callbacks_without_losing_new_history() -> None:
    native = _Native()
    owner = TransportSessionAuthority(native)
    seen: list[bytes] = []
    registration = owner.subscribe("sample/rebuild", lambda sample: seen.append(sample.payload))
    try:
        old = native.callbacks["sample/rebuild"]
        owner.reconfigure_connect(("tcp/127.0.0.1:18491",))
        assert native.callbacks["sample/rebuild"] is not old
        old(TransportSample("sample/rebuild", b"old"))  # type: ignore[operator]
        _emit(native, "sample/rebuild", b"new")
        _wait_for(lambda: seen == [b"new"])
        assert owner.projection().stale_callbacks >= 1
    finally:
        registration.close()
        owner.close()


def test_close_waits_for_callback_already_inside_native_io() -> None:
    native = _Native()
    entered = threading.Event()
    release = threading.Event()
    delivered = threading.Event()

    def held_put(_key: str, _payload: bytes) -> None:
        entered.set()
        assert release.wait(5)

    native.put = held_put  # type: ignore[attr-defined]
    owner = TransportSessionAuthority(native)

    def callback(_sample: TransportSample) -> None:
        owner.put("sample/out", b"x")
        delivered.set()

    owner.subscribe("sample/in", callback)
    try:
        _emit(native, "sample/in")
        assert entered.wait(2)
        with pytest.raises(TimeoutError, match="callbacks still draining"):
            owner.close(timeout=0.03)
        assert not native.closed
        release.set()
        owner.close(timeout=5.0)
        assert delivered.is_set()
        assert native.closed
    finally:
        release.set()


@pytest.mark.parametrize("terminal", ["retire", "close"])
def test_late_callback_pool_admission_after_terminal_fence_is_not_overflow(
    monkeypatch: pytest.MonkeyPatch, terminal: str,
) -> None:
    native = _Native()
    owner = TransportSessionAuthority(native)
    registration = owner.subscribe("sample/race", lambda _sample: None)
    entered = threading.Event()
    release = threading.Event()
    original_admit = owner._callback_pool.admit
    callback_errors: list[BaseException] = []

    def gated_admit(token, observed):  # type: ignore[no-untyped-def]
        entered.set()
        assert release.wait(3), "admission gate was never released"
        return original_admit(token, observed)

    monkeypatch.setattr(owner._callback_pool, "admit", gated_admit)

    def deliver() -> None:
        try:
            _emit(native, "sample/race")
        except BaseException as error:
            callback_errors.append(error)

    callback_thread = threading.Thread(target=deliver, daemon=True)
    callback_thread.start()
    try:
        assert entered.wait(2), "callback did not reach the admission gate"
        before = owner.projection()
        if terminal == "retire":
            registration.close()
            assert owner.callback_pool_snapshot().registrations == 0
        else:
            owner.close()
            assert native.closed
        release.set()
        callback_thread.join(2)
        assert not callback_thread.is_alive()
        assert callback_errors == []
        after = owner.projection()
        assert after.rejected == before.rejected
        assert owner._generation_rejected is False
        if terminal == "retire":
            assert after.callbacks_complete is True
    finally:
        release.set()
        callback_thread.join(2)
        if terminal == "retire":
            owner.close()
