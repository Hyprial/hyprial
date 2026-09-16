"""Harness- and transport-neutral seams for the MCP facade."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable

from hyprial.contracts import ipc_errors


class DaemonDisconnected(ConnectionError):
    """The daemon connection ended before a request produced a response."""


# Error-envelope code the daemon returns when a *different* interactive session
# has taken over an actor (last-writer-wins ``session.register``). It is a
# terminal ownership verdict for the losing stdio Channel child: it must stop
# re-registering (which would steal the actor back and cause delivery flap) --
# distinct from ``STALE_SESSION`` (no session registered at all), which stays a
# transient "re-register me" signal so daemon-restart resilience is preserved.
# The value lives in the shared IPC error-code registry; this name is the
# original import surface and is kept for its existing importers.
SESSION_SUPERSEDED_CODE = ipc_errors.SESSION_SUPERSEDED


class DaemonRequestRejected(RuntimeError):
    """A daemon returned an error envelope (as opposed to dropping the socket).

    Subclasses ``RuntimeError`` so existing broad ``except RuntimeError`` handlers
    (including the channel poll loop's daemon-contact retry) keep treating an
    envelope as a survivable failure, while callers that must branch on the
    daemon's ``code`` -- the supersede verdict in particular -- can read
    ``.code`` instead of parsing the message string.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message


@runtime_checkable
class DaemonConnection(Protocol):
    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class DaemonConnectionFactory(Protocol):
    """Creates a short-lived daemon connection for one MCP tool call."""

    def connect(self) -> AbstractAsyncContextManager[DaemonConnection]: ...
