from __future__ import annotations


class WeixinProtocolError(RuntimeError):
    """Typed failure at the iLink protocol boundary."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class WeixinRequestNotSentError(WeixinProtocolError):
    """Local request validation failed before transport I/O began."""


__all__ = ["WeixinProtocolError", "WeixinRequestNotSentError"]
