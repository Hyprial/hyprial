"""HTTP surface error vocabulary, status mapping and dispatch classification.

Every code here is part of ``contract/http-request``.  ``dispatch`` is chosen by
the stage that produced the failure, never by the code: the same domain code
can be a local pre-dispatch refusal or a daemon response that the client must
treat differently.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from hyprial.kernel import ipc_errors

Dispatch = Literal["not-dispatched", "dispatched", "unknown"]

STATUS_BY_CODE: Mapping[str, int] = {
    # Local HTTP refusals.
    ipc_errors.INVALID_ARGUMENT: 400,
    ipc_errors.INVALID_REQUEST: 400,
    ipc_errors.INVALID_PARAMS: 400,
    "HTTP_HOST_DENIED": 403,
    "HTTP_ORIGIN_DENIED": 403,
    "HTTP_UNAUTHORIZED": 401,
    ipc_errors.METHOD_NOT_FOUND: 404,
    "HTTP_BODY_TOO_LARGE": 413,
    "HTTP_URI_TOO_LONG": 414,
    "HTTP_HEADERS_TOO_LARGE": 431,
    "HTTP_UNSUPPORTED_MEDIA_TYPE": 415,
    "HTTP_METHOD_NOT_ALLOWED": 405,
    "HTTP_BUSY": 429,
    "HTTP_LENGTH_REQUIRED": 411,
    "HTTP_TIMEOUT": 408,
    "HTTP_NOT_READY": 503,
    "HTTP_IDEMPOTENCY_KEY_UNSUPPORTED": 400,
    "HTTP_INTERNAL": 500,
    "PRINCIPAL_MISMATCH": 403,
    # Catalog / version refusals.
    "CATALOG_MISMATCH": 409,
    "CATALOG_STALE": 503,
    ipc_errors.VERSION_MISMATCH: 409,
    # Daemon transport and dispatch results.
    ipc_errors.DAEMON_UNAVAILABLE: 503,
    ipc_errors.DAEMON_RESTORING: 503,
    ipc_errors.DAEMON_NOT_READY: 503,
    ipc_errors.DAEMON_DISCONNECTED: 503,
    ipc_errors.IPC_TIMEOUT: 504,
    "INVALID_RESPONSE": 502,
    "IPC_RESPONSE_TOO_LARGE": 502,
    ipc_errors.DAEMON_ERROR: 500,
    ipc_errors.CALLER_NOT_AUTHORIZED: 403,
    ipc_errors.SENDER_NOT_AUTHENTICATED: 403,
    ipc_errors.SENDER_UNRESOLVED: 403,
    ipc_errors.TARGET_NOT_AUTHORIZED: 403,
    ipc_errors.CHANNEL_NOT_AUTHORIZED: 403,
    ipc_errors.ORG_CONTEXT_NOT_AUTHORIZED: 403,
    ipc_errors.STALE_SESSION: 403,
    ipc_errors.SESSION_SUPERSEDED: 403,
    ipc_errors.INVALID_SESSION_SOURCE: 403,
}

#: Codes a safe-read client may retry after backoff (the server itself never
#: retries).  ``dispatch`` is not a retry permit on its own.
RETRYABLE_CODES = frozenset(
    {
        "HTTP_BUSY",
        "HTTP_NOT_READY",
        ipc_errors.DAEMON_NOT_READY,
        ipc_errors.DAEMON_RESTORING,
        ipc_errors.DAEMON_UNAVAILABLE,
        ipc_errors.DAEMON_DISCONNECTED,
        ipc_errors.IPC_TIMEOUT,
        "INVALID_RESPONSE",
        "IPC_RESPONSE_TOO_LARGE",
    }
)


class HttpFailure(Exception):
    """One HTTP refusal with its stable code, status and dispatch stage."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        dispatch: Dispatch = "not-dispatched",
        data: Any | None = None,
    ) -> None:
        self.code = code
        self.message = _message(message)
        self.status = STATUS_BY_CODE.get(code, 500) if status is None else status
        self.dispatch = dispatch
        self.data = data
        super().__init__(self.message)

    @property
    def outcome_unknown(self) -> bool:
        return self.dispatch == "unknown"


def _message(value: str) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text[:512]


def status_for(code: str, default: int = 500) -> int:
    return STATUS_BY_CODE.get(code, default)


def retryable(code: str) -> bool:
    return code in RETRYABLE_CODES


__all__ = [
    "Dispatch",
    "HttpFailure",
    "RETRYABLE_CODES",
    "STATUS_BY_CODE",
    "retryable",
    "status_for",
]
