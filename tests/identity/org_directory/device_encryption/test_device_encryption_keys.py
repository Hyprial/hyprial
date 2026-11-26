"""Service grant S1: the paired optional ``encryptionKeyId`` / ``encryptionPublicKey``.

A directory device row may carry the device's X25519 public key so another
member can seal a service credential to this device.  The key set stays
closed: the two keys are either both present or both absent, and the id is
the digest of the published public key.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from hyprial.identity.impl.org_directory.models import DirectoryDevice

PUBLIC_KEY = base64.b64encode(bytes(range(32))).decode("ascii")
KEY_ID = "x25519:" + hashlib.sha256(bytes(range(32))).hexdigest()[:32]


def _row(**fields: object) -> dict[str, object]:
    return {
        "deviceId": "device-a",
        "owner": "alice",
        "serverPublic": "nodekey:" + "1" * 64,
        "clientPublic": "nodekey:" + "2" * 64,
        "clientPublicByServer": {},
        "address": "tcp://10.0.0.1:7447#psk",
        "keyGeneration": 1,
        "updatedAt": "2026-11-25T00:00:00Z",
        **fields,
    }


def test_a_row_without_the_pair_keeps_its_exact_shape() -> None:
    record = _row()
    device = DirectoryDevice.from_record(record)
    assert device.encryption_key_id is None
    assert device.encryption_public_key is None
    assert device.as_record() == record
    assert "encryptionKeyId" not in device.as_record()
    assert "encryptionPublicKey" not in device.as_record()


def test_a_row_with_the_pair_round_trips() -> None:
    record = _row(encryptionKeyId=KEY_ID, encryptionPublicKey=PUBLIC_KEY)
    device = DirectoryDevice.from_record(record)
    assert device.encryption_key_id == KEY_ID
    assert device.encryption_public_key == PUBLIC_KEY
    assert device.as_record() == record


def _true_id(public_key: str) -> str:
    return "x25519:" + hashlib.sha256(base64.b64decode(public_key)).hexdigest()[:32]


def test_the_key_id_must_be_the_digest_of_the_published_public_key() -> None:
    """Every sealer names the recipient by key id; a mismatch seals to nobody.

    Both halves are individually well formed, so only the relationship
    between them can refuse this row.
    """
    wrong = "x25519:" + "a" * 32
    assert wrong != KEY_ID
    with pytest.raises(ValueError, match="encryptionKeyId does not match encryptionPublicKey"):
        DirectoryDevice.from_record(_row(encryptionKeyId=wrong, encryptionPublicKey=PUBLIC_KEY))


def test_a_store_generated_pair_is_accepted() -> None:
    from hyprial.identity.impl.user_key.encryption.crypto import generate

    key_id, public_key, _private = generate()
    device = DirectoryDevice.from_record(
        _row(encryptionKeyId=key_id, encryptionPublicKey=public_key)
    )
    assert device.encryption_key_id == key_id == _true_id(public_key)


@pytest.mark.parametrize(
    "lone",
    [
        pytest.param({"encryptionKeyId": KEY_ID}, id="key-id-only"),
        pytest.param({"encryptionPublicKey": PUBLIC_KEY}, id="public-key-only"),
    ],
)
def test_half_of_the_pair_is_rejected(lone: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="encryptionKeyId and encryptionPublicKey"):
        DirectoryDevice.from_record(_row(**lone))


def test_a_dataclass_instance_with_half_of_the_pair_is_rejected() -> None:
    with pytest.raises(ValueError, match="encryptionKeyId and encryptionPublicKey"):
        DirectoryDevice(
            device_id="device-a",
            owner="alice",
            server_public="nodekey:" + "1" * 64,
            client_public="nodekey:" + "2" * 64,
            address="tcp://10.0.0.1:7447#psk",
            key_generation=1,
            updated_at="2026-11-25T00:00:00Z",
            encryption_key_id=KEY_ID,
        )


@pytest.mark.parametrize(
    "key_id",
    [
        pytest.param(None, id="null"),
        pytest.param("", id="empty"),
        pytest.param(7, id="integer"),
        pytest.param("ed25519:" + "a" * 32, id="wrong-algorithm"),
        pytest.param("x25519:" + "A" * 32, id="upper-case-digest"),
        pytest.param("x25519:" + "a" * 31, id="short-digest"),
        pytest.param("x25519:" + "a" * 33, id="long-digest"),
        pytest.param("a" * 32, id="missing-prefix"),
    ],
)
def test_a_malformed_key_id_is_rejected(key_id: object) -> None:
    with pytest.raises(ValueError, match="encryptionKeyId"):
        DirectoryDevice.from_record(
            _row(encryptionKeyId=key_id, encryptionPublicKey=PUBLIC_KEY)
        )


@pytest.mark.parametrize(
    "public_key",
    [
        pytest.param(None, id="null"),
        pytest.param("", id="empty"),
        pytest.param(7, id="integer"),
        pytest.param("not base64!", id="not-base64"),
        pytest.param(base64.b64encode(bytes(31)).decode("ascii"), id="31-bytes"),
        pytest.param(base64.b64encode(bytes(33)).decode("ascii"), id="33-bytes"),
        pytest.param(base64.b64encode(bytes(32)).decode("ascii") + "\n", id="padded-newline"),
        pytest.param(base64.b64encode(bytes(32)).decode("ascii")[:43], id="truncated"),
        pytest.param(bytes(32).hex(), id="hex-not-base64"),
    ],
)
def test_a_public_key_that_is_not_32_bytes_of_canonical_base64_is_rejected(
    public_key: object,
) -> None:
    with pytest.raises(ValueError, match="encryptionPublicKey"):
        DirectoryDevice.from_record(_row(encryptionKeyId=KEY_ID, encryptionPublicKey=public_key))


@pytest.mark.parametrize(
    "public_key",
    [
        pytest.param(base64.b64encode(bytes(32)).decode("ascii"), id="all-zero"),
        pytest.param(
            base64.b64encode((1).to_bytes(32, "little")).decode("ascii"), id="low-order-1"
        ),
    ],
)
def test_a_low_order_point_is_rejected_as_a_public_key(public_key: str) -> None:
    """A low-order point makes every X25519 shared secret public; never publish one."""

    with pytest.raises(ValueError, match="encryptionPublicKey"):
        DirectoryDevice.from_record(_row(encryptionKeyId=KEY_ID, encryptionPublicKey=public_key))


def test_the_pair_does_not_relax_the_closed_key_set() -> None:
    with pytest.raises(ValueError, match="unknown keys: \\['encryptionKeyPrivate'\\]"):
        DirectoryDevice.from_record(
            _row(
                encryptionKeyId=KEY_ID,
                encryptionPublicKey=PUBLIC_KEY,
                encryptionKeyPrivate="never-published",
            )
        )


@pytest.mark.parametrize(
    "key_id, public_key, message",
    [
        pytest.param("x25519:" + "a" * 32, PUBLIC_KEY, "does not match", id="id-not-the-digest"),
        pytest.param(KEY_ID, "not base64!", "must be base64", id="public-key-not-base64"),
        pytest.param(
            KEY_ID,
            base64.b64encode(bytes(31)).decode("ascii"),
            "canonical base64 of 32 bytes",
            id="public-key-wrong-length",
        ),
        pytest.param(
            KEY_ID,
            base64.b64encode(bytes(32)).decode("ascii"),
            "low-order point",
            id="public-key-low-order",
        ),
        pytest.param("ed25519:" + "a" * 32, PUBLIC_KEY, "encryptionKeyId must be", id="id-wrong-shape"),
    ],
)
def test_a_directly_built_row_refuses_an_inconsistent_pair(
    key_id: str, public_key: str, message: str
) -> None:
    """``from_record`` validates, but ``DirectoryDevice(...)`` must not leak a bad row."""

    with pytest.raises(ValueError, match=message):
        DirectoryDevice(
            device_id="device-a",
            owner="alice",
            server_public="nodekey:" + "1" * 64,
            client_public="nodekey:" + "2" * 64,
            address="tcp://10.0.0.1:7447#psk",
            key_generation=1,
            updated_at="2026-11-25T00:00:00Z",
            encryption_key_id=key_id,
            encryption_public_key=public_key,
        )
