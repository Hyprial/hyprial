"""QQ wire error surface; never carries AppSecret or remote response bodies."""

from __future__ import annotations


class QQProtocolError(RuntimeError):
    """Sanitized protocol failure identified by a stable reason code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class QQTokenUnavailable(QQProtocolError):
    """The cached token expired and a fresh fetch failed; callers must block."""


class QQFatalGatewayClose(QQProtocolError):
    """4914/4915: the platform barred this bot; reconnecting is pointless."""


class QQGatewayClosedClean(QQProtocolError):
    """Remote close code 1000: official policy says do not reconnect."""


class QQMsgSeqLimit(QQProtocolError):
    """Source-A passive budget exhausted for one inbound msg_id."""


__all__ = [
    "QQFatalGatewayClose",
    "QQGatewayClosedClean",
    "QQMsgSeqLimit",
    "QQProtocolError",
    "QQTokenUnavailable",
]
