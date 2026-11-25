"""The formal probe admits only a local operator and never acquires saved credentials."""

from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError, ipc_errors

METHOD = "agent.secret.provider-test"
PARAMS = {"provider": "deepseek", "secret": "synthetic-secret", "model": "test-model"}


def test_method_declares_billable_no_retry_write_and_secret_input():
    binding = METHOD_REGISTRY.get(METHOD)
    assert binding is not None
    spec = binding.spec
    assert spec.mutates and spec.retry == "never"
    assert spec.audience == ("local-operator",)
    assert spec.http_exposed and spec.http_binding.identity.mode == "none"
    assert spec.params_schema["properties"]["secret"]["writeOnly"]
    assert spec.params_schema["additionalProperties"] is False


@pytest.mark.parametrize("origin", [None, "worker", "daemon"])
def test_untrusted_origin_is_refused_without_network(origin):
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(SimpleNamespace(), METHOD, PARAMS, trusted_message_origin=origin)
    assert caught.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_registered_interactive_peer_is_refused():
    app = SimpleNamespace(_ipc_peer=SimpleNamespace(pid=1), _interactive_identity_binding_peer=lambda pid: True)
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(app, METHOD, PARAMS, trusted_message_origin="ipc")
    assert caught.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_invalid_input_is_redacted():
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(SimpleNamespace(), METHOD, dict(PARAMS, secret="private\nsecret"),
                               trusted_message_origin="ipc")
    assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    assert "private" not in str(caught.value)


def test_capacity_refuses_without_sending_and_is_released_after_failure(monkeypatch):
    from hyprial.daemon.impl.request_catalog.setup import probe as handler
    from hyprial.daemon.impl import model_probe
    import threading

    capacity = threading.BoundedSemaphore(1)
    monkeypatch.setattr(handler, "_CAPACITY", capacity)
    calls = []

    async def fail(params):
        calls.append(True)
        raise RuntimeError(PARAMS["secret"])

    monkeypatch.setattr(model_probe, "probe", fail)
    capacity.acquire()
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(SimpleNamespace(), METHOD, PARAMS, trusted_message_origin="ipc")
    assert caught.value.code == ipc_errors.RATE_LIMITED
    assert calls == []
    capacity.release()
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(SimpleNamespace(), METHOD, PARAMS, trusted_message_origin="ipc")
    assert caught.value.code == ipc_errors.DAEMON_ERROR
    assert PARAMS["secret"] not in str(caught.value)
    assert capacity.acquire(blocking=False)
    capacity.release()
