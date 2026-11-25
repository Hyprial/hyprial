from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .errors import WeComProtocolError


class WeComCallbackCrypto:
    """Official WeCom SHA-1 + AES-CBC callback envelope."""

    def __init__(
        self,
        token: str,
        encoding_aes_key: str,
        receive_id: str,
        *,
        replay_limit: int = 4096,
    ) -> None:
        if not token or not receive_id:
            raise WeComProtocolError("missing-callback-credentials")
        try:
            key = base64.b64decode(encoding_aes_key + "=", validate=True)
        except (ValueError, TypeError) as exc:
            raise WeComProtocolError("invalid-encoding-aes-key") from exc
        if len(key) != 32:
            raise WeComProtocolError("invalid-encoding-aes-key")
        self._token = token
        self._key = key
        self._receive_id = receive_id.encode("utf-8")
        self._replays: dict[str, None] = {}
        self._replay_limit = min(max(replay_limit, 128), 65_536)

    def signature(self, timestamp: str, nonce: str, encrypted: str) -> str:
        parts = sorted((self._token, str(timestamp), str(nonce), encrypted))
        return hashlib.sha1("".join(parts).encode("utf-8"), usedforsecurity=False).hexdigest()

    def _decrypt(self, encrypted: str) -> bytes:
        try:
            ciphertext = base64.b64decode(encrypted, validate=True)
        except (ValueError, TypeError) as exc:
            raise WeComProtocolError("invalid-callback-ciphertext") from exc
        if not ciphertext or len(ciphertext) % 16:
            raise WeComProtocolError("invalid-callback-ciphertext")
        decryptor = Cipher(algorithms.AES(self._key), modes.CBC(self._key[:16])).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = PKCS7(256).unpadder()
        try:
            plain = unpadder.update(padded) + unpadder.finalize()
        except ValueError as exc:
            raise WeComProtocolError("callback-decryption-failed") from exc
        if len(plain) < 20:
            raise WeComProtocolError("callback-plaintext-truncated")
        length = struct.unpack("!I", plain[16:20])[0]
        end = 20 + length
        if end > len(plain):
            raise WeComProtocolError("callback-plaintext-truncated")
        if not hmac.compare_digest(plain[end:], self._receive_id):
            raise WeComProtocolError("callback-receive-id-mismatch")
        return plain[20:end]

    def verify_and_decrypt(
        self, *, timestamp: str, nonce: str, encrypted: str, signature: str
    ) -> bytes:
        expected = self.signature(timestamp, nonce, encrypted)
        if not hmac.compare_digest(expected, signature.lower()):
            raise WeComProtocolError("callback-signature-mismatch")
        replay_key = hashlib.sha256(
            b"wecom-callback-replay\0"
            + timestamp.encode()
            + b"\0"
            + nonce.encode()
            + b"\0"
            + encrypted.encode()
        ).hexdigest()
        if replay_key in self._replays:
            raise WeComProtocolError("callback-replay")
        plaintext = self._decrypt(encrypted)
        self._replays[replay_key] = None
        if len(self._replays) > self._replay_limit:
            del self._replays[next(iter(self._replays))]
        return plaintext

    def encrypt_for_test(self, plaintext: bytes, *, random_prefix: bytes | None = None) -> str:
        prefix = random_prefix if random_prefix is not None else os.urandom(16)
        if len(prefix) != 16:
            raise ValueError("random_prefix must be 16 bytes")
        raw = prefix + struct.pack("!I", len(plaintext)) + plaintext + self._receive_id
        padder = PKCS7(256).padder()
        padded = padder.update(raw) + padder.finalize()
        encryptor = Cipher(algorithms.AES(self._key), modes.CBC(self._key[:16])).encryptor()
        return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


__all__ = ["WeComCallbackCrypto"]
