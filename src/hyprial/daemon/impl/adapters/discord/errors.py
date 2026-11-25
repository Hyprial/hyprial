"""Discord adapter error surface."""

from __future__ import annotations

from hyprial.daemon.impl.adapters.common.transport import WebSocketCloseObservation


class DiscordProtocolError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class DiscordFatalCloseError(DiscordProtocolError):
    """The gateway demanded a terminal close; the instance must block."""

    def __init__(self, code: int) -> None:
        self.close_code = code
        super().__init__("discord-gateway-fatal-close", f"gateway closed fatally with {code}")


class DiscordConnectionClosed(RuntimeError):
    """A normalized close observation from the current socket generation."""

    def __init__(self, observation: WebSocketCloseObservation) -> None:
        self.observation = observation
        super().__init__(
            f"discord gateway closed: generation={observation.generation} "
            f"origin={observation.origin.value} rcvd={observation.rcvd_code}"
        )


class DiscordMediaScopeError(ValueError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


__all__ = [
    "DiscordConnectionClosed",
    "DiscordFatalCloseError",
    "DiscordMediaScopeError",
    "DiscordProtocolError",
]
