"""Canonical transcript, signature, proof and request-key tests.

The fixed P-256 private keys below are test-only vectors.  They are never
production keys and are never logged by the library or these tests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from hyprial.shell.impl.gui.adapter.mobile.pairing import (
    CryptoError,
    PairingError,
    b64url_decode,
    b64url_encode,
    canonical_json,
    challenge_transcript,
    channel_binding,
    client_proof,
    code_hash,
    decode_public_key,
    derive_request_key,
    redeem_transcript,
    sign_challenge,
    status_transcript,
    verify_challenge,
    verify_client_proof,
)

NODE = "node:dev000000001.local"
PIN = "sha256:" + "ab" * 32
CLIENT_PRIVATE_KEY = ec.derive_private_key(int("11" * 32, 16), ec.SECP256R1())
HOST_SIGNING_PRIVATE_KEY = ec.derive_private_key(int("22" * 32, 16), ec.SECP256R1())
HOST_DERIVE_PRIVATE_KEY = ec.derive_private_key(int("33" * 32, 16), ec.SECP256R1())
CLIENT_NONCE_BYTES = bytes(range(32))
HOST_NONCE_BYTES = bytes(range(32, 64))
PAIRING_ATTEMPT_BYTES = bytes(range(64, 96))
OWNER = "agent:chloe.martin@example.com:dev000000001.local:mini-gui-worker"
REQUEST_ID = "pair-" + "cd" * 16

#: Independently computed from the profile in the contract (P-256 ECDH +
#: HKDF-SHA256) with the fixed test keys above.
EXPECTED_REQUEST_KEY_HEX = (
    "688bfebbf86b08d5c7fa4bf5d2ab2abbf3bd1e67d3d2e4cb7e48cf3ffe308fa4"
)
EXPECTED_CLIENT_PUBLIC = (
    "BAIX5hfwtkQ5KCePlpmeaaI6TywVK99tbN9m5bgCgtTtGUp968uXcS0t2jyoWqh2Wlb0X8dYWZZS8ol8ZTBuV5Q"
)
EXPECTED_HOST_DERIVE_PUBLIC = (
    "BFGnWAgziY6hsYPL1zUKQJkHjG7xweGOlwzXaDA18l59ARBSJxKwtafP8IFoVIaYSpTmgx7axG5zYPqdg0p6gaE"
)
EXPECTED_CHALLENGE_SHA256 = (
    "fcff1254774515b40df3d3b76d52e8489a2188027e5a1e97fdf50c2311822fcd"
)
EXPECTED_REDEEM_SHA256 = (
    "7c569402479573825f81c532db9c10b92dc3e2dd32c6a363fb3b25adf68c7fe2"
)
EXPECTED_STATUS_SHA256 = (
    "3cde28bc7f78698ac5659f8aa28c731232e10c397115d422dc65631453a5c488"
)


def _raw(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


CLIENT_PUBLIC = b64url_encode(_raw(CLIENT_PRIVATE_KEY.public_key()))
HOST_DERIVE_PUBLIC = b64url_encode(_raw(HOST_DERIVE_PRIVATE_KEY.public_key()))
CLIENT_NONCE = b64url_encode(CLIENT_NONCE_BYTES)
HOST_NONCE = b64url_encode(HOST_NONCE_BYTES)
PAIRING_ATTEMPT_ID = b64url_encode(PAIRING_ATTEMPT_BYTES)


def _challenge() -> bytes:
    return challenge_transcript(
        node_id=NODE,
        host_key_fingerprint=PIN,
        client_nonce=CLIENT_NONCE,
        host_nonce=HOST_NONCE,
        client_public=CLIENT_PUBLIC,
        host_derive_public=HOST_DERIVE_PUBLIC,
        server_time=1_000_000,
    )


def _binding() -> str:
    return channel_binding(_challenge())


def _redeem(*, mode: str = "start", code: str | None = "ABCDEFGHJK") -> bytes:
    return redeem_transcript(
        mode=mode,
        channel_binding=_binding(),
        pairing_attempt_id=PAIRING_ATTEMPT_ID,
        code=code,
        device_id="dev-a",
        device_uid="uid-a",
        name="mate phone",
        owner=OWNER,
        client_public=CLIENT_PUBLIC,
        permissions=["sessions.read"],
    )


def _status() -> bytes:
    return status_transcript(
        channel_binding=_binding(),
        request_id=REQUEST_ID,
        pairing_attempt_id=PAIRING_ATTEMPT_ID,
    )


def test_test_keys_and_public_values_are_fixed() -> None:
    assert CLIENT_PUBLIC == EXPECTED_CLIENT_PUBLIC
    assert HOST_DERIVE_PUBLIC == EXPECTED_HOST_DERIVE_PUBLIC
    assert len(CLIENT_PUBLIC) == 87
    assert len(HOST_DERIVE_PUBLIC) == 87


def test_canonical_json_is_sorted_compact_and_deterministic() -> None:
    first = canonical_json(
        {"permissions": ["sessions.write", "sessions.read"], "b": 1, "a": "x"}
    )
    second = canonical_json(
        {"a": "x", "b": 1, "permissions": ["sessions.write", "sessions.read"]}
    )
    assert first == second
    assert first == (
        b'{"a":"x","b":1,"permissions":["sessions.write","sessions.read"]}'
    )
    assert canonical_json({"a": "é"}).decode("utf-8") == '{"a":"é"}'


def test_challenge_transcript_is_canonical_and_domain_separated() -> None:
    first = _challenge()
    second = _challenge()
    assert first == second
    assert hashlib.sha256(first).hexdigest() == EXPECTED_CHALLENGE_SHA256
    assert first.startswith(b'{"clientNonce":')
    assert b'"purpose":"m11-pairing-challenge-v1"' in first
    assert b"m11-pairing-redeem-v1" not in first

    binding = channel_binding(first)
    assert binding == "sha256:" + EXPECTED_CHALLENGE_SHA256
    assert binding.startswith("sha256:")


def test_redeem_and_status_transcripts_are_deterministic() -> None:
    assert hashlib.sha256(_redeem()).hexdigest() == EXPECTED_REDEEM_SHA256
    assert hashlib.sha256(_status()).hexdigest() == EXPECTED_STATUS_SHA256
    assert _redeem() != _status()
    assert b'"purpose":"m11-pairing-redeem-v1"' in _redeem()
    assert b'"purpose":"m11-pairing-status-v1"' in _status()


def test_redeem_transcript_covers_mode_code_and_permissions() -> None:
    reordered = redeem_transcript(
        mode="start",
        channel_binding=_binding(),
        pairing_attempt_id=PAIRING_ATTEMPT_ID,
        code="ABCDEFGHJK",
        device_id="dev-a",
        device_uid="uid-a",
        name="mate phone",
        owner=OWNER,
        client_public=CLIENT_PUBLIC,
        permissions=["sessions.read"],
    )
    assert _redeem() == reordered

    with pytest.raises(PairingError):
        redeem_transcript(
            mode="resume",
            channel_binding=_binding(),
            pairing_attempt_id=PAIRING_ATTEMPT_ID,
            code="ABCDEFGHJK",
            device_id="dev-a",
            device_uid="uid-a",
            name="mate phone",
            owner=OWNER,
            client_public=CLIENT_PUBLIC,
            permissions=["sessions.read"],
        )
    with pytest.raises(PairingError):
        redeem_transcript(
            mode="start",
            channel_binding=_binding(),
            pairing_attempt_id=PAIRING_ATTEMPT_ID,
            code=None,
            device_id="dev-a",
            device_uid="uid-a",
            name="mate phone",
            owner=OWNER,
            client_public=CLIENT_PUBLIC,
            permissions=["sessions.read"],
        )

    resume = _redeem(mode="resume", code=None)
    assert resume != _redeem()
    assert b'"purpose":"m11-pairing-resume-v1"' in resume
    assert b"codeHash" not in resume


def test_code_hash_normalizes_pairing_code() -> None:
    expected = (
        "sha256:f57cbf1e3e471b25db4b8ad0844df41b2dae9c7d1944944d128e49294d412f66"
    )
    assert code_hash("ABCDEFGHJK") == expected
    assert code_hash("abcd-efgh jk") == expected


def test_fixed_test_keys_reproduce_expected_request_key() -> None:
    client_key = derive_request_key(
        CLIENT_PRIVATE_KEY,
        peer_public=HOST_DERIVE_PUBLIC,
        host_derive_public=HOST_DERIVE_PUBLIC,
        client_public=CLIENT_PUBLIC,
    )
    host_key = derive_request_key(
        HOST_DERIVE_PRIVATE_KEY,
        peer_public=CLIENT_PUBLIC,
        host_derive_public=HOST_DERIVE_PUBLIC,
        client_public=CLIENT_PUBLIC,
    )
    assert client_key == host_key
    assert client_key.hex() == EXPECTED_REQUEST_KEY_HEX
    assert len(client_key) == 32


def test_public_key_decoding_is_strict() -> None:
    decoded = decode_public_key(CLIENT_PUBLIC)
    assert _raw(decoded) == _raw(CLIENT_PRIVATE_KEY.public_key())
    with pytest.raises(CryptoError):
        decode_public_key(b64url_encode(b"\x04" + b"\x00" * 64))
    with pytest.raises(CryptoError):
        decode_public_key("not-base64-%%%")
    with pytest.raises(CryptoError):
        b64url_decode("AA==")


def test_challenge_signature_roundtrip_and_tamper() -> None:
    transcript = _challenge()
    signature = sign_challenge(HOST_SIGNING_PRIVATE_KEY, transcript)
    assert len(b64url_decode(signature)) == 64
    verify_challenge(
        HOST_SIGNING_PRIVATE_KEY.public_key(), transcript, signature
    )

    tampered_transcript = transcript.replace(b'"serverTime":1000000', b'"serverTime":1000001')
    with pytest.raises(CryptoError):
        verify_challenge(
            HOST_SIGNING_PRIVATE_KEY.public_key(),
            tampered_transcript,
            signature,
        )

    raw_signature = bytearray(b64url_decode(signature))
    raw_signature[-1] ^= 0x01
    with pytest.raises(CryptoError):
        verify_challenge(
            HOST_SIGNING_PRIVATE_KEY.public_key(),
            transcript,
            b64url_encode(bytes(raw_signature)),
        )


def test_client_proof_roundtrip_and_tamper() -> None:
    request_key = bytes.fromhex(EXPECTED_REQUEST_KEY_HEX)
    transcript = _redeem()
    proof = client_proof(request_key, transcript)
    assert len(b64url_decode(proof)) == 32
    verify_client_proof(request_key, transcript, proof)

    tampered = bytearray(b64url_decode(proof))
    tampered[0] ^= 0x80
    with pytest.raises(PairingError) as caught:
        verify_client_proof(
            request_key, transcript, b64url_encode(bytes(tampered))
        )
    assert caught.value.code == "pairing-client-proof-invalid"


def test_pairing_proof_is_not_interchangeable_with_n_a2_mac() -> None:
    request_key = bytes.fromhex(EXPECTED_REQUEST_KEY_HEX)
    proof = client_proof(request_key, _redeem())
    n_a2_preimage = (
        f"2|{NODE}|1|1000000|nonce-1|space-a|sessions.list|"
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    ).encode()
    n_a2_mac = base64.urlsafe_b64encode(
        hmac.new(request_key, n_a2_preimage, hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    assert proof != n_a2_mac
    assert proof != client_proof(request_key, _status())
    assert _redeem() != _challenge()
    assert _status() != _challenge()


def test_client_proof_rejects_wrong_key_and_shape() -> None:
    request_key = bytes.fromhex(EXPECTED_REQUEST_KEY_HEX)
    transcript = _redeem()
    proof = client_proof(request_key, transcript)
    with pytest.raises(PairingError):
        verify_client_proof(b"\x00" * 32, transcript, proof)
    with pytest.raises(PairingError):
        verify_client_proof(request_key, transcript, "not-a-proof")
    with pytest.raises(PairingError):
        client_proof(b"short", transcript)
