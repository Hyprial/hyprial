from __future__ import annotations

from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from hyprial.daemon.impl.adapters.contracts import (
    AuthorizedByteStream,
    NativeTargetResolution,
    OperationNode,
    PartSubmission,
    SealedProviderMediaRef,
)

from .errors import WeixinProtocolError


class WeixinMedia:
    """Media port; unavailable unless a receiver-owned sealed authority is injected."""

    def __init__(self, authority: object | None = None) -> None:
        self._authority = authority

    async def fetch(
        self, source: SealedProviderMediaRef, max_bytes: int
    ) -> AuthorizedByteStream:
        fetch = getattr(self._authority, "fetch", None)
        if not callable(fetch):
            raise WeixinProtocolError("media-fetch-unavailable")
        if max_bytes < 1:
            raise WeixinProtocolError("invalid-media-limit")
        return await fetch(source, max_bytes)

    async def upload(
        self,
        operation: OperationNode,
        target: NativeTargetResolution,
        stream: AuthorizedByteStream,
    ) -> PartSubmission:
        upload = getattr(self._authority, "upload", None)
        if not callable(upload):
            raise WeixinProtocolError("media-upload-unavailable")
        return await upload(operation, target, stream)


def trusted_weixin_cdn_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise WeixinProtocolError("invalid-media-url") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "novac2c.cdn.weixin.qq.com"
        or parsed.port not in (None, 443)
        or not parsed.path.startswith("/c2c/")
        or parsed.username is not None
    ):
        raise WeixinProtocolError("untrusted-media-url")
    return value


def decrypt_weixin_media(ciphertext: bytes, key: bytes, *, max_bytes: int) -> bytes:
    if len(key) != 16 or not ciphertext or len(ciphertext) % 16:
        raise WeixinProtocolError("invalid-media-ciphertext")
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    try:
        plaintext = unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise WeixinProtocolError("media-decryption-failed") from exc
    if len(plaintext) > max_bytes:
        raise WeixinProtocolError("media-too-large")
    return plaintext


__all__ = ["WeixinMedia", "decrypt_weixin_media", "trusted_weixin_cdn_url"]
