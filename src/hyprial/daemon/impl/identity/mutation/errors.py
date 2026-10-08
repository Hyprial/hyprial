"""The identity resolver's one caller-facing error (a frozen §5 code + text)."""

from __future__ import annotations


class IdentityResolverError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


__all__ = ["IdentityResolverError"]
