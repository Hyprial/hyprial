"""Stateless, reconnecting daemon proxy used by MCP tools."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .api import DaemonConnectionFactory, DaemonDisconnected


class StatelessDaemonProxy:
    """Run every tool call against a fresh daemon connection.

    Mutation calls retain one request ID across the single reconnect retry.  A
    daemon adapter can therefore deduplicate the ambiguous "accepted, then the
    socket closed" case without the MCP process keeping inbox or session state.
    """

    def __init__(
        self, factory: DaemonConnectionFactory, *, reconnect_attempts: int = 1
    ) -> None:
        if reconnect_attempts < 0:
            raise ValueError("reconnect_attempts must not be negative")
        self._factory = factory
        self._reconnect_attempts = reconnect_attempts

    async def call(
        self,
        *,
        actor: str,
        session_ref: str,
        method: str,
        params: dict[str, Any],
        mutation: bool,
    ) -> dict[str, Any]:
        request_id = str(uuid4()) if mutation else None
        request = {
            **params,
            "actor": actor,
            "sessionRef": session_ref,
        }
        for attempt in range(self._reconnect_attempts + 1):
            try:
                async with self._factory.connect() as connection:
                    return await connection.request(
                        method, request, request_id=request_id
                    )
            except DaemonDisconnected:
                if attempt >= self._reconnect_attempts:
                    raise
        raise AssertionError("unreachable reconnect loop")
