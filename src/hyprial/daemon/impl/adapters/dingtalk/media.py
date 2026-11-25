from __future__ import annotations

from urllib.parse import urlsplit

from hyprial.daemon.impl.adapters.contracts import (
    AuthorizedByteStream,
    NativeTargetResolution,
    OperationNode,
    PartSubmission,
    SealedProviderMediaRef,
)

from .errors import DingTalkProtocolError


class DingTalkMedia:
    def __init__(self, authority: object | None = None) -> None:
        self._authority = authority

    async def fetch(
        self, source: SealedProviderMediaRef, max_bytes: int
    ) -> AuthorizedByteStream:
        fetch = getattr(self._authority, "fetch", None)
        if not callable(fetch):
            raise DingTalkProtocolError("media-fetch-unavailable")
        if max_bytes < 1:
            raise DingTalkProtocolError("invalid-media-limit")
        return await fetch(source, max_bytes)

    async def upload(
        self,
        operation: OperationNode,
        target: NativeTargetResolution,
        stream: AuthorizedByteStream,
    ) -> PartSubmission:
        upload = getattr(self._authority, "upload", None)
        if not callable(upload):
            raise DingTalkProtocolError("media-upload-unavailable")
        return await upload(operation, target, stream)


def trusted_dingtalk_media_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not (host == "dingtalk.com" or host.endswith(".dingtalk.com"))
        or parsed.port not in (None, 443)
        or parsed.username is not None
    ):
        raise DingTalkProtocolError("untrusted-media-url")
    return value


__all__ = ["DingTalkMedia", "trusted_dingtalk_media_url"]
