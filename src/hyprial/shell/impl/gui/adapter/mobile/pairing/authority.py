"""The narrow authority seam used by the S1 state machine.

This module intentionally contains no SQLite, schema or lifecycle
implementation.  S2 binds the real store (pairing-attempt uniqueness, code
consumption and projections in one transaction) behind this protocol; S1
tests use a pure in-memory fake.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol


class AuthorityError(Exception):
    """An internal authority refusal, never serialised directly."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        retry_after_seconds: int | None = None,
        **details: object,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.retry_after_seconds = retry_after_seconds
        self.details: dict[str, object] = dict(details)
        if field is not None:
            self.details.setdefault("field", field)
        if retry_after_seconds is not None:
            self.details.setdefault("retryAfter", retry_after_seconds)


class PairingAuthority(Protocol):
    """The authoritative operations the pairing state machine may request."""

    def redeem_pairing_code(
        self,
        *,
        code: str | None,
        device_id: str,
        device_uid: str,
        name: str,
        owner: str,
        client_public: str,
        permissions: Sequence[str],
        pairing_attempt_id: str,
        mode: str,
        now: float | None = None,
    ) -> Mapping[str, Any]:
        """Return the request record or raise :class:`AuthorityError`."""

    def pairing_status(
        self,
        request_id: str,
        *,
        pairing_attempt_id: str,
        now: float | None = None,
    ) -> Mapping[str, Any]:
        """Return the current request record or raise :class:`AuthorityError`."""
