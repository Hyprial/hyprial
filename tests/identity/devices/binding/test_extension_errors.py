"""Known lazy extension errors keep the existing public failure contracts.

All material is a normal synthetic chain. Only the certificate property/loader
is replaced; no malformed encoding or independent diagnostic probe is used.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import ExtensionOID

from hyprial.identity.interface import (
    TransportClientError,
    TransportCredentialError,
    binding_claims,
    commit_transport_credential,
    load_client_tls_material,
    read_transport_credential,
    validate_enrollment_response,
    verify_certificate_binding,
)
from tests.identity.devices.test_transport_credential import _response, _stage

from . import support

DETAIL = "synthetic extension decoder detail"
DECODING_ERRORS = (
    pytest.param(
        lambda: x509.DuplicateExtension(DETAIL, ExtensionOID.BASIC_CONSTRAINTS),
        id="duplicate-extension",
    ),
    pytest.param(lambda: x509.UnsupportedGeneralNameType(DETAIL), id="unsupported-name"),
    pytest.param(lambda: ValueError(DETAIL), id="value-error"),
)


class _UnreadableExtensions:
    """Delegate a real certificate except its lazily decoded extensions."""

    def __init__(self, certificate, make_error):
        self._certificate = certificate
        self._make_error = make_error

    def __getattr__(self, name):
        return getattr(self._certificate, name)

    @property
    def extensions(self):
        raise self._make_error()


def _material(home: Path, *, committed: bool = False):
    staged = _stage(home)
    response, bundle = _response(staged, include_intermediate=True)
    credential = validate_enrollment_response(
        response,
        staged=staged,
        pinned_trust_bundle_pem=bundle,
        effective_owner=staged.owner,
    )
    if committed:
        commit_transport_credential(
            home, staged=staged, credential=credential, pinned_trust_bundle_pem=bundle
        )
    return staged, response, bundle, credential


def _patch_decoding(monkeypatch, response, bundle, target, make_error):
    original = x509.load_pem_x509_certificates
    if target == "leaf":
        selected = original(response["certificatePem"].encode("ascii"))[0]
    elif target == "root":
        selected = original(bundle)[0]
    else:
        selected = original(response["chainPem"].encode("ascii"))[0]
    expected = selected.fingerprint(hashes.SHA256())

    def load(encoded):
        return [
            _UnreadableExtensions(cert, make_error)
            if cert.fingerprint(hashes.SHA256()) == expected
            else cert
            for cert in original(encoded)
        ]

    monkeypatch.setattr(x509, "load_pem_x509_certificates", load)


def _claims(credential):
    return binding_claims(
        policy_issuer=credential.policy_issuer,
        binding_id=credential.binding_id,
        owner_user_id=credential.owner_user_id,
        owner=credential.owner,
        device_uid=credential.device_uid,
        credential_generation=credential.credential_generation,
        spki_sha256=credential.spki_sha256,
        renewable_until=credential.renewable_until,
        trust_bundle_id=credential.trust_bundle_id,
    )


def _assert_sanitized(error):
    assert DETAIL not in str(error)
    assert DETAIL not in repr(error)
    assert error.__context__ is None
    assert error.__cause__ is None


def _call(operation, home, staged, response, bundle, credential):
    if operation == "validate":
        return validate_enrollment_response(
            response,
            staged=staged,
            pinned_trust_bundle_pem=bundle,
            effective_owner=staged.owner,
        )
    if operation == "commit":
        return commit_transport_credential(
            home, staged=staged, credential=credential, pinned_trust_bundle_pem=bundle
        )
    if operation == "read":
        return read_transport_credential(home)
    return load_client_tls_material(home)


@pytest.mark.parametrize("make_error", DECODING_ERRORS)
def test_public_binding_normalizes_lazy_extension_errors(tmp_path, make_error):
    _staged, response, _bundle, credential = _material(tmp_path)
    leaf = x509.load_pem_x509_certificate(response["certificatePem"].encode("ascii"))
    certificate = _UnreadableExtensions(leaf, make_error)
    with pytest.raises(TransportCredentialError) as caught:
        verify_certificate_binding(certificate, _claims(credential))
    assert caught.value.code == "TRANSPORT_RESPONSE_INVALID"
    _assert_sanitized(caught.value)


@pytest.mark.parametrize("make_error", DECODING_ERRORS)
@pytest.mark.parametrize("target", ["leaf", "root", "issuer"])
@pytest.mark.parametrize("operation", ["validate", "commit", "read", "material"])
def test_public_credential_paths_normalize_lazy_extension_errors(
    tmp_path, monkeypatch, make_error, target, operation
):
    staged, response, bundle, credential = _material(
        tmp_path, committed=operation in ("read", "material")
    )
    before = support.inventory(tmp_path)
    _patch_decoding(monkeypatch, response, bundle, target, make_error)
    if operation == "read":
        assert _call(operation, tmp_path, staged, response, bundle, credential) is None
    else:
        error_type = TransportClientError if operation == "material" else TransportCredentialError
        with pytest.raises(error_type) as caught:
            _call(operation, tmp_path, staged, response, bundle, credential)
        expected = "TRANSPORT_CREDENTIAL_ABSENT" if operation == "material" else "TRANSPORT_RESPONSE_INVALID"
        assert caught.value.code == expected
        if operation == "material":
            assert caught.value.outcome == "NOT_SENT"
            assert caught.value.retryable is False
        _assert_sanitized(caught.value)
    assert support.inventory(tmp_path) == before


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("operation", ["binding", "validate", "commit", "read", "material"])
def test_lazy_extension_boundary_preserves_control_signals(
    tmp_path, monkeypatch, signal, operation
):
    staged, response, bundle, credential = _material(
        tmp_path, committed=operation in ("read", "material")
    )
    before = support.inventory(tmp_path)
    with pytest.raises(signal):
        if operation == "binding":
            leaf = x509.load_pem_x509_certificate(response["certificatePem"].encode("ascii"))
            verify_certificate_binding(_UnreadableExtensions(leaf, lambda: signal(DETAIL)), _claims(credential))
        else:
            _patch_decoding(monkeypatch, response, bundle, "leaf", lambda: signal(DETAIL))
            _call(operation, tmp_path, staged, response, bundle, credential)
    assert support.inventory(tmp_path) == before


@pytest.mark.parametrize("operation", ["read", "material"])
def test_unsafe_storage_still_precedes_extension_decoding(tmp_path, monkeypatch, operation):
    staged, response, bundle, credential = _material(tmp_path, committed=True)
    (tmp_path / "secrets/transport/device-key.pem").chmod(0o640)
    before = support.inventory(tmp_path)
    _patch_decoding(monkeypatch, response, bundle, "leaf", lambda: x509.UnsupportedGeneralNameType(DETAIL))
    error_type = TransportClientError if operation == "material" else TransportCredentialError
    with pytest.raises(error_type) as caught:
        _call(operation, tmp_path, staged, response, bundle, credential)
    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    if operation == "material":
        assert caught.value.outcome == "NOT_SENT"
        assert caught.value.retryable is False
    assert support.inventory(tmp_path) == before
