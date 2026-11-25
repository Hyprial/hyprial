"""Typed mapping outcomes survive removal of private exception chains."""

import inspect

import pytest

from hyprial.identity.impl.device.client import http
from hyprial.identity.interface import ClientTlsMaterial, DeviceTransportClient

from . import support as s


@pytest.mark.parametrize("site,failure,code,outcome,retryable,operation", s.HTTP_FAILURES)
def test_http_mapping_detaches_private_context(
    tmp_path, monkeypatch, site, failure, code, outcome, retryable, operation
):
    private = s.marker(tmp_path)
    config = s.material_config() if site == "chain" else s.config()
    with monkeypatch.context() as scoped:
        faults = s.inject(scoped, site, private, failure)
        result = s.capture(lambda: s.retire(DeviceTransportClient(config)), private)
    assert result == s.expected(code, outcome, retryable, operation)
    if site in {"raw-timeout", "handshake", "write-timeout", "send", "recv"}:
        assert faults.raw.closed or faults.tls.closed


@pytest.mark.parametrize("module,name", [("tempfile", "mkdtemp"), ("os", "chmod"),
                                         ("os", "open"), ("os", "write")])
def test_material_setup_maps_and_detaches(tmp_path, monkeypatch, module, name):
    private = s.marker(tmp_path)
    config = s.material_config()
    with monkeypatch.context() as scoped:
        faults = s.inject(scoped, "material", private)
        if (module, name) == ("os", "chmod"):
            scoped.setattr(
                http,
                "harden_private_path",
                lambda *_args, **_kwargs: faults.raise_at("material"),
            )
        else:
            scoped.setattr(http, module, s.FaultModule(getattr(http, module), name, faults))
        result = s.capture(lambda: s.retire(DeviceTransportClient(config)), private)
    assert result == s.expected("TRANSPORT_CLIENT_CONFIG_INVALID")


@pytest.mark.parametrize("site", ["config-ascii", "csr-ascii"])
def test_public_validation_unicode_mapping_detaches(tmp_path, site):
    private = s.marker(tmp_path)
    if site == "config-ascii":
        def call():
            return s.config(trust_bundle_pem=b"\xff" + private.encode())
        code = "TRANSPORT_CLIENT_CONFIG_INVALID"
    else:
        client = DeviceTransportClient(s.config())
        def call():
            return client.request_certificate(
                operation_id=s.OPERATION, device_uid=s.DEVICE,
                expected_credential_generation=0, csr_pem=private + "é",
            )
        code = "TRANSPORT_REQUEST_INVALID"
    assert s.capture(call, private) == s.expected(code)


@pytest.mark.parametrize("site", ["read-timeout", "close"])
def test_existing_generic_post_send_boundary_remains_clean(tmp_path, monkeypatch, site):
    private = s.marker(tmp_path)
    with monkeypatch.context() as scoped:
        s.inject(scoped, site, private, RuntimeError)
        result = s.capture(lambda: s.retire(DeviceTransportClient(s.config())), private)
    assert result == s.expected("TRANSPORT_RESPONSE_INVALID", "UNKNOWN", True,
                                s.OPERATION, reason_code="POST_SEND_EXCEPTION",
                                exception_class="RuntimeError")


@pytest.mark.parametrize("first", ["UNKNOWN", "REJECTED"])
def test_not_sent_exact_replay_restores_previous_state(tmp_path, monkeypatch, first):
    private = s.marker(tmp_path)
    with monkeypatch.context() as scoped:
        faults = s.inject(scoped, "recv" if first == "UNKNOWN" else "unused", private)
        client = DeviceTransportClient(s.config())
        initial = s.capture(lambda: s.retire(client), private)
        assert initial["outcome"] == first
        previous = client._attempts[s.OPERATION]
        faults.site = "connect"
        retry = s.capture(lambda: s.retire(client), private)
        assert retry == s.expected("AUTHORITY_UNAVAILABLE", retryable=True)
        assert client._attempts[s.OPERATION] is previous
        next_result = s.capture(lambda: s.retire(client, s.NEXT_OPERATION), private)
    if first == "UNKNOWN":
        assert next_result == s.expected("TRANSPORT_OUTCOME_UNRESOLVED", "UNKNOWN",
                                         operation_id=s.OPERATION)
    else:
        assert next_result == s.expected("AUTHORITY_UNAVAILABLE", retryable=True)


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("site", ["connect", "recv"])
def test_control_flow_signals_are_preserved(tmp_path, monkeypatch, signal, site):
    private = s.marker(tmp_path)
    with monkeypatch.context() as scoped:
        s.inject(scoped, site, private, signal)
        result = s.capture(lambda: s.retire(DeviceTransportClient(s.config())), private)
    assert result == {"unexpected_exception_class": signal.__name__}


def test_public_callable_metadata_is_preserved():
    assert DeviceTransportClient.retire.__name__ == "retire"
    assert tuple(inspect.signature(DeviceTransportClient.retire).parameters) == (
        "self", "operation_id", "binding_id", "expected_credential_generation"
    )
    assert ClientTlsMaterial.__post_init__.__name__ == "__post_init__"
