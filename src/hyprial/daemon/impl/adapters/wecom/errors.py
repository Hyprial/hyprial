from __future__ import annotations


class WeComProtocolError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


__all__ = ["WeComProtocolError"]
