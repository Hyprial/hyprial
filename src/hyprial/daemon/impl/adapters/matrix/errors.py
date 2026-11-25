"""Typed Matrix adapter failures."""

from __future__ import annotations


class MatrixAdapterError(RuntimeError):
    """Base failure raised by Matrix protocol logic."""


class MatrixCryptoBlocked(MatrixAdapterError):
    """An encrypted effect is blocked because audited crypto is not ready."""


class MatrixRateLimited(MatrixAdapterError):
    """Provider refusal carrying an exact retry delay."""

    def __init__(self, retry_after_ms: int) -> None:
        if isinstance(retry_after_ms, bool) or not isinstance(retry_after_ms, int):
            raise TypeError("retry_after_ms must be an integer")
        if retry_after_ms < 0:
            raise ValueError("retry_after_ms must not be negative")
        self.retry_after_ms = retry_after_ms
        super().__init__(f"Matrix rate limited for {retry_after_ms}ms")


class MatrixDefinitelyFailed(MatrixAdapterError):
    """Provider rejection that proves the event was not submitted."""

    def __init__(self, reason_code: str) -> None:
        if not isinstance(reason_code, str) or not reason_code:
            raise ValueError("reason_code is required")
        self.reason_code = reason_code
        super().__init__(reason_code)


__all__ = [
    "MatrixAdapterError",
    "MatrixCryptoBlocked",
    "MatrixDefinitelyFailed",
    "MatrixRateLimited",
]
