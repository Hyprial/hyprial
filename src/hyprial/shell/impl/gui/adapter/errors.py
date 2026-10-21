"""Failure vocabulary of the App-facing chat adapter (node side).

Two failure classes, mirroring the browser Core's send semantics: a
definitive rejection the caller must surface as *failed* (never retried with
a new identity or payload), and an unavailable/unknown outcome the caller
must surface as *uncertain* -- neither success nor failure.
"""

from __future__ import annotations

from typing import Any

#: The operation is outside the adapter's v0 surface; fail closed.
UNSUPPORTED_OPERATION = "unsupported-operation"
#: The request is malformed before any daemon call happened.
INVALID_ARGUMENT = "invalid-argument"
#: The session id is not bound by this adapter instance.
UNKNOWN_SESSION = "unknown-session"
#: The target the caller named is not in the daemon's current agent directory.
UNKNOWN_TARGET = "unknown-target"
#: The caller tried to supply identity/authority fields; the adapter's
#: sender is fixed by the trusted composition root, never by the request.
IDENTITY_REJECTED = "identity-rejected"
#: The daemon definitively refused the send (no identity/payload change of
#: the caller's can turn this into a success).
DAEMON_REJECTED = "daemon-rejected"
#: The daemon's answer proves the message was not accepted.
SEND_REJECTED = "send-rejected"


class AppAdapterError(Exception):
    """A definitive, non-retryable rejection (Core renders it as failed)."""

    def __init__(self, code: str, message: str, details: Any | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details if details is not None else {}


class AppAdapterUnavailable(Exception):
    """The outcome cannot be determined (transport, timeout, unknown submit).

    The caller must not treat this as failure or success; the same
    ``idempotencyKey`` may be used to retry or to query the outcome.
    """

    def __init__(self, reason: str, details: Any | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details if details is not None else {}
