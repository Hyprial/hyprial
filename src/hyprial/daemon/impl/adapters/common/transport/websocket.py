"""One-shot WebSocket opening and lossless close observations."""

from __future__ import annotations

from typing import Any

import websockets
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedOK,
    InvalidHandshake,
    InvalidProxy,
    InvalidURI,
)

from .types import (
    WebSocketCloseObservation,
    WebSocketCloseOrigin,
    WebSocketLocalClose,
)


_OPENING_ERRORS = (InvalidURI, InvalidProxy, InvalidHandshake, OSError, TimeoutError, EOFError)


class WebSocketOpenError(ConnectionError):
    def __init__(self, observation: WebSocketCloseObservation) -> None:
        super().__init__(f"websocket opening failed: {observation.opening_error}")
        self.observation = observation


def _opening_observation(error: Exception, generation: int) -> WebSocketCloseObservation:
    return WebSocketCloseObservation(
        generation=generation,
        origin=WebSocketCloseOrigin.OPENING,
        clean=False,
        rcvd_code=None,
        rcvd_reason=None,
        sent_code=None,
        sent_reason=None,
        rcvd_then_sent=None,
        local_code=None,
        local_reason=None,
        opening_error=type(error).__name__,
    )


async def open_websocket_once(
    uri: str,
    *,
    generation: int,
    open_timeout: float,
    close_timeout: float,
    max_size: int = 1024 * 1024,
    additional_headers: Any = None,
) -> Any:
    """Open exactly once with proxy and library reconnect behavior disabled."""

    if generation < 1:
        raise ValueError("generation must be positive")
    if open_timeout <= 0 or close_timeout <= 0:
        raise ValueError("WebSocket timeouts must be positive")
    try:
        return await websockets.connect(
            uri,
            proxy=None,
            open_timeout=open_timeout,
            close_timeout=close_timeout,
            max_size=max_size,
            additional_headers=additional_headers,
        )
    except _OPENING_ERRORS as error:
        raise WebSocketOpenError(_opening_observation(error, generation)) from error


def observe_connection_closed(
    error: ConnectionClosed,
    *,
    generation: int,
    local_intent: WebSocketLocalClose | None = None,
) -> WebSocketCloseObservation:
    if not isinstance(error, ConnectionClosed):
        raise TypeError("error must be websockets.ConnectionClosed")
    if generation < 1:
        raise ValueError("generation must be positive")
    received = error.rcvd
    sent = error.sent
    if local_intent is not None:
        origin = WebSocketCloseOrigin.LOCAL
    elif received is not None:
        origin = WebSocketCloseOrigin.REMOTE
    else:
        origin = WebSocketCloseOrigin.TRANSPORT
    return WebSocketCloseObservation(
        generation=generation,
        origin=origin,
        clean=isinstance(error, ConnectionClosedOK),
        rcvd_code=received.code if received is not None else None,
        rcvd_reason=received.reason if received is not None else None,
        sent_code=sent.code if sent is not None else None,
        sent_reason=sent.reason if sent is not None else None,
        rcvd_then_sent=error.rcvd_then_sent,
        local_code=local_intent.code if local_intent is not None else None,
        local_reason=local_intent.reason if local_intent is not None else None,
        opening_error=None,
    )


__all__ = [
    "WebSocketOpenError",
    "observe_connection_closed",
    "open_websocket_once",
]
