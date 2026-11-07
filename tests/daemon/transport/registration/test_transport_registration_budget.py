"""Transport declarations have bounded metadata distinct from callback actors."""

from __future__ import annotations

import socket
import threading
import time
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.transport.api import TransportSample
from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport
from hyprial.daemon.impl.transport.session_actor import TransportSessionAuthority


class Registration:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class Native:
    config = SimpleNamespace(connect=())

    def __init__(self) -> None:
        self.registrations: list[Registration] = []
        self.callbacks: list[object] = []
        self.closed = False

    def _register(self, callback: object | None = None) -> Registration:
        handle = Registration()
        self.registrations.append(handle)
        if callback is not None:
            self.callbacks.append(callback)
        return handle

    def declare_liveliness(self, _key: str) -> Registration:
        return self._register()

    def declare_queryable(self, _key: str, callback) -> Registration:
        return self._register(callback)

    def subscribe(self, _key: str, callback) -> Registration:
        return self._register(callback)

    def observe_liveliness(self, _key: str, callback, *, history: bool = True) -> Registration:
        return self._register(callback)

    def close(self) -> None:
        self.closed = True


def test_320_route_pairs_fit_bounded_declarations_without_callback_actors() -> None:
    native = Native()
    owner = TransportSessionAuthority(native)
    registrations = []
    try:
        for index in range(320):
            registrations.append(owner.declare_liveliness(f"actor/{index}"))
            registrations.append(
                owner.declare_queryable(f"inbox/{index}", lambda _selector: b"ok")
            )
        assert len(registrations) == 640
        assert owner.projection().registrations == 640
        assert len(owner._callbacks) == 640
        assert owner.callback_pool_snapshot().registrations == 0
        assert len(native.registrations) == 640
    finally:
        for registration in reversed(registrations):
            registration.close()
        owner.close()
    assert native.closed


def test_declaration_limit_is_exact_and_retire_releases_credit() -> None:
    native = Native()
    owner = TransportSessionAuthority(
        native, declaration_capacity=4, callback_registration_capacity=1
    )
    registrations = []
    try:
        for index in range(2):
            registrations.append(owner.declare_liveliness(f"actor/{index}"))
            registrations.append(owner.declare_queryable(f"inbox/{index}", lambda _s: b"ok"))
        assert len(owner._callbacks) == 4
        with pytest.raises(RuntimeError, match="declaration capacity exhausted"):
            owner.declare_liveliness("actor/overflow")
        assert len(native.registrations) == 4
        registrations.pop().close()
        registrations.pop().close()
        assert len(owner._callbacks) == 2
        registrations.append(owner.declare_liveliness("actor/replacement"))
        registrations.append(owner.declare_queryable("inbox/replacement", lambda _s: b"ok"))
        assert len(owner._callbacks) == 4
    finally:
        for registration in reversed(registrations):
            registration.close()
        owner.close()


def test_retired_callback_actor_keeps_credit_until_callback_finishes() -> None:
    native = Native()
    owner = TransportSessionAuthority(
        native, declaration_capacity=8, callback_registration_capacity=1
    )
    entered = threading.Event()
    release = threading.Event()

    def held(_sample: TransportSample) -> None:
        entered.set()
        assert release.wait(3)

    first = owner.subscribe("sample/held", held)
    second = None
    try:
        assert owner.callback_pool_snapshot().registrations == 1
        native.callbacks[-1](TransportSample("sample/held", b"x"))
        assert entered.wait(1)
        first.close()
        assert owner.callback_pool_snapshot().retired == 1
        with pytest.raises(RuntimeError, match="callback registration capacity exhausted"):
            owner.subscribe("sample/overflow", lambda _sample: None)
        release.set()
        deadline = time.monotonic() + 2
        while second is None and time.monotonic() < deadline:
            try:
                second = owner.subscribe("sample/recovered", lambda _sample: None)
            except RuntimeError as error:
                assert "callback registration capacity exhausted" in str(error)
                time.sleep(0.01)
        assert second is not None
        assert owner.callback_pool_snapshot().registrations == 1
    finally:
        release.set()
        if second is not None:
            second.close()
        owner.close()


def test_failed_callback_actor_start_releases_only_unaccepted_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = Native()
    owner = TransportSessionAuthority(
        native, declaration_capacity=4, callback_registration_capacity=1
    )
    original_register = owner._callback_pool.register

    def fail_callback(_token: str):
        raise RuntimeError("synthetic callback registration failure")

    monkeypatch.setattr(owner._callback_pool, "register", fail_callback)
    try:
        with pytest.raises(RuntimeError, match="synthetic callback registration failure"):
            owner.subscribe("sample/fail", lambda _sample: None)
        assert owner._callbacks == {}
        assert owner.callback_pool_snapshot().registrations == 0
        assert native.registrations == []
        monkeypatch.setattr(owner._callback_pool, "register", original_register)
        registration = owner.subscribe("sample/recovered", lambda _sample: None)
        registration.close()
    finally:
        owner.close()


@pytest.mark.parametrize("kind", ["declare_queryable", "subscribe"])
def test_observational_timeout_retains_accepted_native_registration(kind) -> None:
    native = Native()
    entered = threading.Event()
    release = threading.Event()
    original_declare = getattr(native, kind)

    def held_declare(key, callback):
        entered.set()
        assert release.wait(3)
        return original_declare(key, callback)

    setattr(native, kind, held_declare)
    owner = TransportSessionAuthority(native, timeout=0.03, declaration_capacity=4)
    try:
        with pytest.raises(TimeoutError, match="remains accepted"):
            getattr(owner, kind)("inbox/held", lambda _selector: b"ok")
        assert entered.is_set()
        assert len(owner._callbacks) == 1
        assert owner.callback_pool_snapshot().registrations == int(kind == "subscribe")
        release.set()
        deadline = time.monotonic() + 2
        while not native.registrations and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(native.registrations) == 1
        assert len(owner._callbacks) == 1
    finally:
        release.set()
        owner.close()


def test_real_loopback_native_declarations_share_control_owner_budget() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        endpoint = f"tcp/127.0.0.1:{probe.getsockname()[1]}"
    native = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    owner = TransportSessionAuthority(
        native, declaration_capacity=16, callback_registration_capacity=1
    )
    registrations = []
    try:
        for index in range(4):
            registrations.append(owner.declare_liveliness(f"hyprial/test/budget/actor/{index}"))
            registrations.append(
                owner.declare_queryable(
                    f"hyprial/test/budget/inbox/{index}", lambda _selector: b"ok"
                )
            )
        assert owner.projection().registrations == 8
        assert len(owner._callbacks) == 8
        assert owner.callback_pool_snapshot().registrations == 0
        assert len(native._query_registrations) == 4
    finally:
        for registration in reversed(registrations):
            registration.close()
        owner.close()
    assert native._native_closed is True
