"""Typed, non-secret failures for the one-request device authority client."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

_P = ParamSpec("_P")
_T = TypeVar("_T")

OUTCOME_NOT_SENT = "NOT_SENT"
OUTCOME_REJECTED = "REJECTED"
OUTCOME_UNKNOWN = "UNKNOWN"

OUTCOMES = (OUTCOME_NOT_SENT, OUTCOME_REJECTED, OUTCOME_UNKNOWN)

# Stable client-only codes.  Authority codes are preserved verbatim instead.
REQUEST_INVALID = "TRANSPORT_REQUEST_INVALID"
REQUEST_TOO_LARGE = "TRANSPORT_REQUEST_TOO_LARGE"
CLIENT_CONFIG_INVALID = "TRANSPORT_CLIENT_CONFIG_INVALID"
CREDENTIAL_ABSENT = "TRANSPORT_CREDENTIAL_ABSENT"
REDIRECT_REFUSED = "TRANSPORT_REDIRECT_REFUSED"
RESPONSE_INVALID = "TRANSPORT_RESPONSE_INVALID"
RESPONSE_UNKNOWN = "TRANSPORT_RESPONSE_UNKNOWN"
REPLAY_MISMATCH = "TRANSPORT_REPLAY_MISMATCH"
OUTCOME_UNRESOLVED = "TRANSPORT_OUTCOME_UNRESOLVED"
POST_SEND_EXCEPTION = "POST_SEND_EXCEPTION"


class TransportClientError(RuntimeError):
    """A typed client failure that never carries key, PEM, CSR or path text.

    ``code`` is either a client-only constant above or the exact authority
    envelope code.  ``outcome`` distinguishes "the request never left the
    machine" from "the authority rejected it" and from "the request was sent
    and the outcome is unknown, so only an exact replay settles it".
    """

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        outcome: str = OUTCOME_REJECTED,
        http_status: int | None = None,
        operation_id: str | None = None,
        reason_code: str | None = None,
        exception_class: str | None = None,
    ) -> None:
        if outcome not in OUTCOMES:
            raise ValueError("invalid transport client outcome")
        if reason_code is not None and reason_code != POST_SEND_EXCEPTION:
            raise ValueError("invalid transport client reason code")
        if exception_class is not None and (
            not exception_class.isascii()
            or not exception_class.isidentifier()
            or len(exception_class) > 128
        ):
            raise ValueError("invalid transport client exception class")
        self.code = code
        self.retryable = bool(retryable)
        self.outcome = outcome
        self.http_status = http_status
        self.operation_id = operation_id
        self.reason_code = reason_code
        self.exception_class = exception_class
        super().__init__(code)

    def __repr__(self) -> str:
        return (
            f"TransportClientError(code={self.code!r}, retryable={self.retryable!r}, "
            f"outcome={self.outcome!r}, http_status={self.http_status!r}, "
            f"reason_code={self.reason_code!r}, "
            f"exception_class={self.exception_class!r})"
        )


def _public_error_boundary(function: Callable[_P, _T]) -> Callable[_P, _T]:
    """Detach mapped exception chains after replay/state handling completes."""

    @wraps(function)
    def detached(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        try:
            return function(*args, **kwargs)
        except TransportClientError as error:
            # ``raise ... from None`` only suppresses context display. Remove
            # both public links while preserving this error and its fields.
            error.__context__ = None
            error.__cause__ = None
            raise

    return detached
