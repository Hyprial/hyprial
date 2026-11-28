"""Sealed service-grant record tests: round trip, custody, and refusals."""

from __future__ import annotations

import base64
import json
import logging
import stat
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from hyprial.daemon.impl.service_connect.grants import (
    GrantStore,
    MAX_SECRET_BYTES,
    grant_service,
    open_grant,
    parse_recipient_key,
    seal_grant,
)
from hyprial.daemon.impl.service_connect.models import ServiceConnectError
from hyprial.kernel import ipc_errors


def _key_pair() -> tuple[bytes, bytes, str]:
    private = X25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    return private.private_bytes_raw(), public, parse_recipient_key(
        "x25519:" + public.hex()
    )[0]


def test_round_trip_never_persists_plaintext(tmp_path) -> None:
    private, public, key_id = _key_pair()
    store = GrantStore(tmp_path / "service-grants")
    secret = b"sk-live-test-secret-value"

    record = store.create(
        service="api",
        device_id="remote",
        recipient_public=public,
        secret=secret,
        created_at=1795443172228,
    )

    path = tmp_path / "service-grants" / f"{record.grant_id}.json"
    raw = path.read_bytes()
    assert secret not in raw
    assert secret.decode() not in raw.decode()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert record.public_view() == {
        "grantId": record.grant_id,
        "service": "api",
        "deviceId": "remote",
        "recipientKeyId": key_id,
        "createdAt": 1795443172228,
    }
    assert "header" not in record.public_view()

    stored = store.read(record.grant_id)
    assert open_grant(stored, private_key=private) == secret


def test_wrong_recipient_cannot_open_the_record(tmp_path) -> None:
    _private, public, _key_id = _key_pair()
    other_private, _other_public, _other_id = _key_pair()
    store = GrantStore(tmp_path / "grants")
    record = store.create(
        service="api",
        device_id="remote",
        recipient_public=public,
        secret=b"secret",
        created_at=1,
    )

    with pytest.raises(ServiceConnectError) as error:
        open_grant(store.read(record.grant_id), private_key=other_private)
    assert error.value.code == ipc_errors.SERVICE_UNAVAILABLE


def test_tampered_ciphertext_is_refused(tmp_path) -> None:
    private, public, _key_id = _key_pair()
    store = GrantStore(tmp_path / "grants")
    record = store.create(
        service="api",
        device_id="remote",
        recipient_public=public,
        secret=b"secret",
        created_at=1,
    )
    stored = store.read(record.grant_id)
    payload = stored["payload"]
    assert isinstance(payload, dict)
    ciphertext = payload["ciphertext"]
    assert isinstance(ciphertext, str)
    payload["ciphertext"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]

    with pytest.raises(ServiceConnectError) as error:
        open_grant(stored, private_key=private)
    assert error.value.code == ipc_errors.SERVICE_UNAVAILABLE


@pytest.mark.parametrize(
    "value",
    [
        "",
        "nodekey:" + "a" * 64,
        "x25519:" + "a" * 63,
        "x25519:" + "A" * 64,
        "x25519:" + "zz" * 32,
    ],
)
def test_recipient_key_must_be_an_explicit_x25519_key(value: str) -> None:
    with pytest.raises(ServiceConnectError) as error:
        parse_recipient_key(value)
    assert error.value.code == ipc_errors.INVALID_ARGUMENT


def test_secret_bounds_are_enforced() -> None:
    _private, public, _key_id = _key_pair()
    header = {
        "grant_id": "sg." + "a" * 26,
        "service": "api",
        "device_id": "remote",
        "recipient_public": public,
        "created_at": 1,
    }

    with pytest.raises(ServiceConnectError) as empty:
        seal_grant(b"", **header)
    assert empty.value.code == ipc_errors.INVALID_ARGUMENT
    with pytest.raises(ServiceConnectError) as too_big:
        seal_grant(b"x" * (MAX_SECRET_BYTES + 1), **header)
    assert too_big.value.code == ipc_errors.INVALID_ARGUMENT


def test_record_read_rejects_a_non_grant_id(tmp_path) -> None:
    store = GrantStore(tmp_path / "grants")
    with pytest.raises(ServiceConnectError) as error:
        store.read("../../etc/passwd")
    assert error.value.code == ipc_errors.INVALID_ARGUMENT


def test_record_file_contains_only_the_envelope_fields(tmp_path) -> None:
    _private, public, _key_id = _key_pair()
    store = GrantStore(tmp_path / "grants")
    record = store.create(
        service="api",
        device_id="remote",
        recipient_public=public,
        secret=b"secret",
        created_at=1,
    )
    value = json.loads((tmp_path / "grants" / f"{record.grant_id}.json").read_text())
    assert set(value) == {"header", "recipient", "payload"}
    assert set(value["header"]) == {
        "v",
        "alg",
        "wrap",
        "grantId",
        "service",
        "deviceId",
        "recipientKeyId",
        "createdAt",
    }


def _catalog(name: str = "api") -> SimpleNamespace:
    entry = SimpleNamespace(name=name, device_id="remote", remote_port=8080)
    return SimpleNamespace(entry=lambda value: entry)


def _request(public: bytes, secret: bytes = b"s3cret-value") -> dict[str, str]:
    return {
        "recipientKey": "x25519:" + public.hex(),
        "secretB64": base64.b64encode(secret).decode("ascii"),
    }


def test_grant_service_refuses_an_unavailable_device_before_sealing(tmp_path) -> None:
    _private, public, _key_id = _key_pair()

    def resolve_device(device_id: str, remote_port: int) -> None:
        raise ServiceConnectError(
            ipc_errors.SERVICE_DEVICE_UNAVAILABLE, "service device is unavailable"
        )

    access = SimpleNamespace(resolve_device=resolve_device)
    with pytest.raises(ServiceConnectError) as error:
        grant_service(
            "api",
            _request(public),
            access,
            _catalog(),
            tmp_path / "service-access.json",
        )
    assert error.value.code == ipc_errors.SERVICE_DEVICE_UNAVAILABLE
    assert not (tmp_path / "service-grants").exists()


def test_grant_service_audits_without_the_secret(tmp_path, caplog) -> None:
    _private, public, key_id = _key_pair()
    access = SimpleNamespace(resolve_device=lambda device_id, remote_port: None)
    secret = b"never-in-the-audit"

    with caplog.at_level(
        logging.INFO, logger="hyprial.daemon.impl.service_connect.grants"
    ):
        result = grant_service(
            "api",
            _request(public, secret),
            access,
            _catalog(),
            tmp_path / "service-access.json",
        )

    receipt = result["grant"]
    assert (tmp_path / "service-grants" / f"{receipt['grantId']}.json").is_file()
    assert receipt["grantId"].startswith("sg.")
    assert receipt["recipientKeyId"] == key_id
    assert set(receipt) == {
        "grantId",
        "service",
        "deviceId",
        "recipientKeyId",
        "createdAt",
    }
    record = caplog.records[-1]
    assert record.event == "service.grant.created"
    assert set(record.__dict__) & {"secret", "secretB64"} == set()
    assert secret.decode() not in " ".join(
        str(value) for value in record.__dict__.values()
    )
