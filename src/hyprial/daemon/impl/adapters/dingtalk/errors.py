from __future__ import annotations


class DingTalkProtocolError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


__all__ = ["DingTalkProtocolError"]
