"""Telegram Bot API adapter implementation."""

from .components import TelegramComponents, build_default_components
from .errors import (
    TelegramError,
    TelegramFilePathExpired,
    TelegramMediaDenied,
    TelegramPollRetry,
    TelegramProtocolError,
)
from .media import MAX_DOWNLOAD_BYTES, TelegramMedia, TelegramMediaScopeResolver
from .provider import (
    TelegramBotRuntime,
    TelegramCheckpointLedger,
    TelegramCredentialResolver,
    TelegramCredentials,
    TelegramEventCodec,
    TelegramOnboarding,
    TelegramTransport,
)
from .transport import (
    ALLOWED_UPDATES,
    OFFICIAL_API_BASE_URL,
    HttpxTelegramTransport,
    TelegramSendOutcome,
)

__all__ = [
    "ALLOWED_UPDATES",
    "MAX_DOWNLOAD_BYTES",
    "OFFICIAL_API_BASE_URL",
    "TelegramBotRuntime",
    "TelegramCheckpointLedger",
    "TelegramCredentialResolver",
    "TelegramCredentials",
    "TelegramEventCodec",
    "TelegramError",
    "TelegramFilePathExpired",
    "TelegramMedia",
    "TelegramMediaDenied",
    "TelegramMediaScopeResolver",
    "TelegramOnboarding",
    "TelegramPollRetry",
    "TelegramProtocolError",
    "TelegramComponents",
    "TelegramSendOutcome",
    "TelegramTransport",
    "HttpxTelegramTransport",
    "build_default_components",
]
