import threading
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.lark.outbound.gateway import GatewayIoAuthority
from hyprial.daemon.impl.squire.addressing import UserAdapterRegistry


def test_registry_keeps_shared_gateway_until_last_alias_and_drains_replacements():
    registry = UserAdapterRegistry()
    first = GatewayIoAuthority(
        SimpleNamespace(send_owner_dm=lambda *args, **kwargs: "first")
    )
    second = GatewayIoAuthority(
        SimpleNamespace(send_owner_dm=lambda *args, **kwargs: "second")
    )
    try:
        registry.register("one", first)
        registry.register("two", first)
        registry.register("one", second)
        assert (
            registry.get("two").send_owner_dm("owner", "text", idempotency_key="id")
            == "first"
        )
        registry.unregister("two")
        with pytest.raises(RuntimeError, match="closed"):
            first.send_owner_dm("owner", "text", idempotency_key="new")
    finally:
        assert registry.close(2)


def test_gateway_timeout_retains_native_custody_and_sdk_has_one_owner():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def send(chat_id, text, *, idempotency_key):
        entered.set()
        assert release.wait(3)
        calls.append((idempotency_key, threading.get_ident()))
        return "native"

    owner = GatewayIoAuthority(
        SimpleNamespace(send_chat=send), capacity=1, timeout=0.01
    )
    try:
        with pytest.raises(TimeoutError, match="remains accepted"):
            owner.send_chat("chat", "text", idempotency_key="one")
        assert entered.is_set()
        with pytest.raises(RuntimeError, match="overloaded"):
            owner.send_chat("chat", "text", idempotency_key="two")
        assert not owner.close(0.01)
    finally:
        release.set()
        assert owner.close(2)
    assert calls[0][0] == "one"
    assert calls[0][1] != threading.get_ident()


def test_original_sdk_exception_is_preserved_at_protocol_edge():
    error = ValueError("synthetic SDK refusal")

    def send(*args, **kwargs):
        raise error

    owner = GatewayIoAuthority(SimpleNamespace(send_owner_dm=send))
    try:
        with pytest.raises(ValueError) as caught:
            owner.send_owner_dm("owner", "text", idempotency_key="request")
        assert caught.value is error
    finally:
        assert owner.close()


def test_gateway_pre_admission_refusal_is_typed():
    owner = GatewayIoAuthority(SimpleNamespace(send_owner_dm=lambda *args, **kwargs: "sent"))
    assert owner.close()

    with pytest.raises(RuntimeError) as caught:
        owner.send_owner_dm("owner", "text", idempotency_key="request")

    assert type(caught.value).__name__ == "GatewayAdmissionRefused"
    assert getattr(caught.value, "reason", None) == "closed"


def test_rejected_fresh_gateway_retains_dependency_cleanup_after_zero_budget(monkeypatch):
    released, dependency_closed = threading.Event(), threading.Event()
    dependency = SimpleNamespace(close=lambda timeout: dependency_closed.set() or True)
    owner = GatewayIoAuthority(SimpleNamespace(), dependents=(dependency,))
    original_stop = owner._runtime.stop

    def delayed_stop(handle, timeout):
        if not released.wait(timeout):
            return False
        return original_stop(handle, timeout)

    monkeypatch.setattr(owner._runtime, "stop", delayed_stop)
    registry = UserAdapterRegistry()
    assert registry.close()
    try:
        with pytest.raises(RuntimeError, match="closed"):
            registry.register("late", owner)
        assert not dependency_closed.is_set()
        released.set()
        # The registry rejected it and never retries; cleanup must still finish.
        assert dependency_closed.wait(2)
        assert owner.close(0.1)
    finally:
        released.set()
        assert owner.close(2)


def test_dependency_close_exception_keeps_cleanup_custody():
    calls = []

    def close(timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise OSError("transient synthetic cleanup failure")
        return True

    owner = GatewayIoAuthority(SimpleNamespace(), dependents=(SimpleNamespace(close=close),))
    assert owner.close(2)
    assert len(calls) == 2
    assert owner._close_error is None
