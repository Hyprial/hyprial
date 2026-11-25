"""Redacted Telegram transport and media failures."""


class TelegramError(RuntimeError):
    """Base error whose message is always a stable, non-secret reason code."""


class TelegramProtocolError(TelegramError):
    pass


class TelegramPollRetry(LookupError, TelegramError):
    """A long-poll cycle ended without an ingress effect and may be retried."""


class TelegramMediaDenied(ValueError, TelegramError):
    pass


class TelegramFilePathExpired(TelegramMediaDenied):
    pass


__all__ = [
    "TelegramError",
    "TelegramFilePathExpired",
    "TelegramMediaDenied",
    "TelegramPollRetry",
    "TelegramProtocolError",
]
