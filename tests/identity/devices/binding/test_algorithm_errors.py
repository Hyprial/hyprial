"""Ordinary UnsupportedAlgorithm stubs retain credential failure contracts."""

from __future__ import annotations

import pytest
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from hyprial.identity.interface import (
    TransportClientError,
    TransportCredentialError,
    stage_transport_credential,
)

from . import support
from .test_extension_errors import _call, _material

DETAIL = "synthetic unsupported algorithm detail"


class _VerifyErrorKey(rsa.RSAPublicKey):
    """Real RSA operations except the selected child's signature verification."""

    def __init__(self, key, signature, make_error):
        self._key, self._signature, self._make_error = key, signature, make_error

    @property
    def key_size(self):
        return self._key.key_size

    def public_bytes(self, *args, **kwargs):
        return self._key.public_bytes(*args, **kwargs)

    def public_numbers(self):
        return self._key.public_numbers()

    def encrypt(self, *args, **kwargs):
        return self._key.encrypt(*args, **kwargs)

    def verify(self, signature, *args, **kwargs):
        if signature == self._signature:
            raise self._make_error()
        return self._key.verify(signature, *args, **kwargs)

    def recover_data_from_signature(self, *args, **kwargs):
        return self._key.recover_data_from_signature(*args, **kwargs)

    def __eq__(self, other):
        return self._key == other

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


class _AlgorithmCertificate:
    def __init__(self, certificate, selected, site, make_error):
        self._certificate = certificate
        self._selected = selected
        self._site = site
        self._make_error = make_error

    def __getattr__(self, name):
        return getattr(self._certificate, name)

    def _matches(self):
        return self._certificate.fingerprint(hashes.SHA256()) == self._selected.fingerprint(hashes.SHA256())

    def public_key(self):
        if self._site == "public-key" and self._matches():
            raise self._make_error()
        key = self._certificate.public_key()
        if self._site == "verify":
            return _VerifyErrorKey(key, self._selected.signature, self._make_error)
        return key

    @property
    def signature_hash_algorithm(self):
        if self._site == "signature-hash" and self._matches():
            raise self._make_error()
        return self._certificate.signature_hash_algorithm


def _patch_certificate(monkeypatch, response, bundle, target, site, make_error):
    original = x509.load_pem_x509_certificates
    if target == "leaf":
        selected = original(response["certificatePem"].encode("ascii"))[0]
    elif target == "root":
        selected = original(bundle)[0]
    else:
        selected = original(response["chainPem"].encode("ascii"))[0]

    def load(encoded):
        return [_AlgorithmCertificate(cert, selected, site, make_error) for cert in original(encoded)]

    monkeypatch.setattr(x509, "load_pem_x509_certificates", load)


def _patch_private_key(monkeypatch, make_error):
    def load(*_args, **_kwargs):
        raise make_error()

    monkeypatch.setattr(serialization, "load_pem_private_key", load)


def _invoke(operation, home, staged, response, bundle, credential):
    if operation == "stage":
        return stage_transport_credential(
            home,
            operation_id=staged.operation_id,
            owner=staged.owner,
            device_uid=staged.device_uid,
            expected_credential_generation=staged.expected_credential_generation,
        )
    return _call(operation, home, staged, response, bundle, credential)


def _assert_rejected(operation, home, material, expected):
    if operation == "read":
        assert _invoke(operation, home, *material) is None
        return
    error_type = TransportClientError if operation == "material" else TransportCredentialError
    with pytest.raises(error_type) as caught:
        _invoke(operation, home, *material)
    error = caught.value
    assert error.code == expected
    assert DETAIL not in str(error)
    assert DETAIL not in repr(error)
    assert error.__context__ is None
    assert error.__cause__ is None
    if operation == "material":
        assert error.outcome == "NOT_SENT"
        assert error.retryable is False


@pytest.mark.parametrize("operation", ["validate", "commit", "read", "material"])
@pytest.mark.parametrize("target", ["leaf", "root", "issuer"])
@pytest.mark.parametrize("site", ["public-key", "signature-hash", "verify"])
def test_certificate_algorithm_failures_are_typed_or_absent(tmp_path, monkeypatch, operation, target, site):
    material = _material(tmp_path, committed=operation in ("read", "material"))
    staged, response, bundle, _credential = material
    before = support.inventory(tmp_path)
    _patch_certificate(monkeypatch, response, bundle, target, site, lambda: UnsupportedAlgorithm(DETAIL))
    expected = "TRANSPORT_CREDENTIAL_ABSENT" if operation == "material" else "TRANSPORT_RESPONSE_INVALID"
    _assert_rejected(operation, tmp_path, material, expected)
    assert support.inventory(tmp_path) == before


@pytest.mark.parametrize("operation", ["stage", "commit", "read", "material"])
def test_private_key_algorithm_failure_is_typed_or_absent(tmp_path, monkeypatch, operation):
    material = _material(tmp_path, committed=operation in ("read", "material"))
    before = support.inventory(tmp_path)
    _patch_private_key(monkeypatch, lambda: UnsupportedAlgorithm(DETAIL))
    expected = "TRANSPORT_CREDENTIAL_ABSENT" if operation == "material" else "TRANSPORT_STAGING_CONFLICT"
    _assert_rejected(operation, tmp_path, material, expected)
    assert support.inventory(tmp_path) == before


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("site", ["public-key", "signature-hash", "verify", "private-stage", "private-material"])
def test_algorithm_boundaries_preserve_control_signals(tmp_path, monkeypatch, signal, site):
    operation = "material" if site == "private-material" else "stage" if site == "private-stage" else "validate"
    material = _material(tmp_path, committed=operation == "material")
    staged, response, bundle, _credential = material
    before = support.inventory(tmp_path)
    if site.startswith("private"):
        _patch_private_key(monkeypatch, lambda: signal(DETAIL))
    else:
        _patch_certificate(monkeypatch, response, bundle, "leaf", site, lambda: signal(DETAIL))
    with pytest.raises(signal):
        _invoke(operation, tmp_path, *material)
    assert support.inventory(tmp_path) == before


@pytest.mark.parametrize("operation", ["read", "material"])
def test_private_algorithm_error_does_not_hide_unsafe_storage(tmp_path, monkeypatch, operation):
    material = _material(tmp_path, committed=True)
    (tmp_path / "secrets/transport/device-key.pem").chmod(0o640)
    before = support.inventory(tmp_path)
    _patch_private_key(monkeypatch, lambda: UnsupportedAlgorithm(DETAIL))
    error_type = TransportClientError if operation == "material" else TransportCredentialError
    with pytest.raises(error_type) as caught:
        _invoke(operation, tmp_path, *material)
    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    if operation == "material":
        assert caught.value.outcome == "NOT_SENT"
    assert support.inventory(tmp_path) == before
