"""Service grant device encryption key: the paired optional ``encryptionKeyId`` / ``encryptionPublicKey``.

A directory device row may carry the device's X25519 public key so another
member can seal a service credential to this device.  The key set stays
closed: the two keys are either both present or both absent, and the id is
the digest of the published public key.
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

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


_P = 2**255 - 19
_LOW_ORDER_U = (
    0,
    1,
    _P - 1,
    39382357235489614581723060781553021112529911719440698176882885853963445705823,
    325606250916557431795983626356110631294008115727848805560023387167927233504,
)


def _encodings(u: int) -> list[bytes]:
    """Every 32-byte string X25519 reads as ``u``, for a canonical ``u < p``.

    Bit 255 is dropped and the value is then reduced modulo ``p``, so one
    point has up to four spellings: canonical, with bit 255 set, one ``p`` too
    large, and both.  A value one ``p`` too large is only the same point while
    bit 255 is still clear, which is why few points have the last two.
    """

    high = 1 << 255
    values = [u, u + high]
    if u + _P < high:
        values += [u + _P, u + _P + high]
    assert all(value < (1 << 256) for value in values)
    return [value.to_bytes(32, "little") for value in values]


def _x25519_refuses(raw: bytes) -> bool:
    """Whether the X25519 in use refuses this public key or yields an all-zero secret.

    That library is the oracle for this test and shares nothing with the
    model.  A low-order peer is refused by some releases and gives a zero
    shared secret on others; both are read as refused.
    """

    try:
        shared = X25519PrivateKey.generate().exchange(X25519PublicKey.from_public_bytes(raw))
    except ValueError:
        return True
    return not any(shared)


def _model_refuses(raw: bytes) -> bool:
    public_key = base64.b64encode(raw).decode("ascii")
    record = _row(encryptionKeyId=_true_id(public_key), encryptionPublicKey=public_key)
    try:
        DirectoryDevice.from_record(record)
    except ValueError as error:
        assert "low-order point" in str(error), str(error)
        return True
    return False


@pytest.mark.parametrize("index", range(len(_LOW_ORDER_U)))
def test_a_low_order_point_is_refused_in_every_spelling(index: int) -> None:
    """A low-order point makes every shared secret public, whichever bytes carry it."""

    spellings = _encodings(_LOW_ORDER_U[index])

    assert len(spellings) >= 2
    assert all(_x25519_refuses(raw) for raw in spellings)
    assert [_model_refuses(raw) for raw in spellings] == [True] * len(spellings)


def test_an_ordinary_point_is_not_mistaken_for_a_low_order_one() -> None:
    """Refusing an ordinary point would refuse a real device's key.

    ``p + 2`` reads as ``2``: the model used to refuse it from a list of raw
    bytes.  The rest are spellings of small ordinary values and keys made by
    the store.
    """

    ordinary = _encodings(2) + _encodings(9) + _encodings(_P + 3)
    for _ in range(8):
        ordinary.append(X25519PrivateKey.generate().public_key().public_bytes_raw())

    assert not any(_x25519_refuses(raw) for raw in ordinary)
    assert not any(_model_refuses(raw) for raw in ordinary)


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
