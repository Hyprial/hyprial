"""Typed, path-free failures for mTLS material loading and socket cleanup."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from hyprial.identity.impl.device.client import http as client_http
from hyprial.identity.interface import (
    ClientTlsMaterial,
    DeviceAuthorityConfig,
    DeviceTransportClient,
    TransportClientError,
)

from ..binding import support as binding_support
from . import support

SECRET_PATH = "/secret/private/path leaked"


class _WritingOs:
    """os proxy that performs real, but incomplete, writes for one payload."""

    def __init__(self, module, *, withhold: int) -> None:
        self._module = module
        self._withhold = withhold
        self._shortened = False
        self.written = 0
        self.attempts: list[tuple[int, int]] = []

    def __getattr__(self, attribute: str):
        if attribute != "write":
            return getattr(self._module, attribute)
        module = self._module
        records = self.attempts

        def _write(descriptor, payload):
            if not self._shortened and self._withhold:
                self._shortened = True
                cut = payload[: len(payload) - self._withhold]
                count = module.write(descriptor, cut)
            else:
                count = module.write(descriptor, payload)
            records.append((len(payload), count))
            self.written += count
            return count

        return _write


def _zero_write(descriptor, payload):
    return 0


class _RaisingModule:
    """Module proxy that fails on one attribute, leaving the real module intact."""

    def __init__(self, module, name: str) -> None:
        self._module = module
        self._name = name

    def __getattr__(self, attribute: str):
        if attribute == self._name:
            def _fail(*_args, **_kwargs):
                raise OSError(SECRET_PATH)

            return _fail
        return getattr(self._module, attribute)


def _config(server, *, client_tls=None):
    return DeviceAuthorityConfig(
        origin=server.origin,
        server_name="127.0.0.1",
        trust_bundle_pem=server.ca.pem,
        connect_timeout=2.0,
        handshake_timeout=2.0,
        read_timeout=2.0,
        write_timeout=2.0,
        client_tls=(
            support.client_material(server.ca)
            if client_tls is None
            else client_tls
        ),
    )


def _assert_typed_and_path_free(error: TransportClientError) -> None:
    assert error.code == "TRANSPORT_CLIENT_CONFIG_INVALID"
    assert error.outcome == "NOT_SENT"
    assert error.retryable is False
    assert "secret" not in str(error)
    assert "secret" not in repr(error)
    assert "/" not in str(error)


def test_temp_directory_failure_is_typed_and_path_free(
    tmp_path: Path, monkeypatch
) -> None:
    staged = binding_support.stage(tmp_path)
    with support.LoopbackAuthority([]) as server:
        monkeypatch.setattr(
            client_http, "tempfile", _RaisingModule(tempfile, "mkdtemp")
        )
        with pytest.raises(TransportClientError) as caught:
            DeviceTransportClient(_config(server)).enroll(staged)
    _assert_typed_and_path_free(caught.value)
    assert server.observation.requests == []


def test_material_write_failure_is_typed_and_path_free(
    tmp_path: Path, monkeypatch
) -> None:
    staged = binding_support.stage(tmp_path)
    with support.LoopbackAuthority([]) as server:
        monkeypatch.setattr(client_http, "os", _RaisingModule(os, "write"))
        with pytest.raises(TransportClientError) as caught:
            DeviceTransportClient(_config(server)).enroll(staged)
    _assert_typed_and_path_free(caught.value)
    assert server.observation.requests == []


def test_material_mode_failure_is_typed_and_path_free(
    tmp_path: Path, monkeypatch
) -> None:
    staged = binding_support.stage(tmp_path)
    with support.LoopbackAuthority([]) as server:
        def _fail(*_args, **_kwargs):
            raise OSError(SECRET_PATH)

        monkeypatch.setattr(client_http, "harden_private_path", _fail)
        with pytest.raises(TransportClientError) as caught:
            DeviceTransportClient(_config(server)).enroll(staged)
    _assert_typed_and_path_free(caught.value)
    assert server.observation.requests == []


def test_chain_load_failure_is_typed_and_path_free(
    tmp_path: Path, monkeypatch
) -> None:
    staged = binding_support.stage(tmp_path)
    with support.LoopbackAuthority([]) as server:
        def _fail(*_args, **_kwargs):
            raise OSError(SECRET_PATH)

        monkeypatch.setattr(client_http.ssl.SSLContext, "load_cert_chain", _fail)
        with pytest.raises(TransportClientError) as caught:
            DeviceTransportClient(_config(server)).enroll(staged)
    _assert_typed_and_path_free(caught.value)
    assert server.observation.requests == []


def test_cleanup_failure_does_not_mask_a_successful_response(
    tmp_path: Path, monkeypatch
) -> None:
    staged = binding_support.stage(tmp_path)
    data = support.certificate_data()
    with support.LoopbackAuthority(
        [
            lambda _request: support.raw_response(
                support.success_envelope(staged.operation_id, data)
            )
        ]
    ) as server:
        monkeypatch.setattr(client_http, "os", _RaisingModule(os, "unlink"))
        assert DeviceTransportClient(_config(server)).enroll(staged) == data
    assert server.observation.requests



def test_partial_material_write_is_retried_until_complete(
    tmp_path: Path, monkeypatch
) -> None:
    """A real short write must not silently truncate the material file.

    The proxy withholds the final byte of the first payload with a real short
    ``os.write``.  A client that writes once leaves the chain PEM one byte
    short; OpenSSL still accepts it, so the exchange succeeds while the material
    on disk is not what the caller supplied.  Every byte must reach the file.
    """

    staged = binding_support.stage(tmp_path)
    data = support.certificate_data()
    with support.LoopbackAuthority(
        [
            lambda _request: support.raw_response(
                support.success_envelope(staged.operation_id, data)
            )
        ],
        require_client_cert=True,
    ) as server:
        material = support.client_material(server.ca)
        proxy = _WritingOs(os, withhold=1)
        monkeypatch.setattr(client_http, "os", proxy)
        assert (
            DeviceTransportClient(_config(server, client_tls=material)).enroll(
                staged
            )
            == data
        )
    assert server.observation.client_certificate_der
    assert proxy.written == len(material.certificate_chain_pem) + len(
        material.private_key_pem
    )
    assert proxy.attempts[0][1] < proxy.attempts[0][0]


def test_zero_progress_material_write_is_typed_and_path_free(
    tmp_path: Path, monkeypatch
) -> None:
    """A write that never makes progress fails typed instead of looping."""

    staged = binding_support.stage(tmp_path)
    with support.LoopbackAuthority([]) as server:
        monkeypatch.setattr(client_http.os, "write", _zero_write)
        with pytest.raises(TransportClientError) as caught:
            DeviceTransportClient(_config(server)).enroll(staged)
    _assert_typed_and_path_free(caught.value)
    assert server.observation.requests == []


INVALID_PUBLIC_MATERIAL = [
    pytest.param("not-bytes", b"key", b"bundle", id="non-bytes-chain"),
    pytest.param(b"chain", "not-bytes", b"bundle", id="non-bytes-key"),
    pytest.param(b"chain", b"key", "not-bytes", id="non-bytes-bundle"),
    pytest.param(b"", b"key", b"bundle", id="empty-chain"),
    pytest.param(b"chain", b"", b"bundle", id="empty-key"),
    pytest.param(b"chain", b"key", b"", id="empty-bundle"),
]


@pytest.mark.parametrize(("chain", "key", "bundle"), INVALID_PUBLIC_MATERIAL)
def test_invalid_public_material_is_typed_and_path_free(
    tmp_path: Path, chain, key, bundle
) -> None:
    """The exported material type rejects invalid bytes at construction."""

    staged = binding_support.stage(tmp_path)
    with support.LoopbackAuthority([]) as server:
        with pytest.raises(TransportClientError) as caught:
            material = ClientTlsMaterial(
                certificate_chain_pem=chain,
                private_key_pem=key,
                trust_bundle_pem=bundle,
            )
            DeviceTransportClient(
                _config(server, client_tls=material)
            ).enroll(staged)
    _assert_typed_and_path_free(caught.value)
    assert server.observation.requests == []
