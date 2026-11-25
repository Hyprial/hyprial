"""macOS iMessage text-DM adapter implementation."""

from .io import (
    IMessageRow,
    IMessageSendObservation,
    OsaScriptSender,
    SQLiteMessageReader,
)
from .runtime import (
    IMessageBlockedError,
    IMessageCodec,
    IMessageLedger,
    IMessageReader,
    IMessageRuntime,
    IMessageSender,
)
from .state import IMessageCursorLedger

__all__ = [
    "IMessageBlockedError",
    "IMessageCodec",
    "IMessageCursorLedger",
    "IMessageLedger",
    "IMessageReader",
    "IMessageRow",
    "IMessageRuntime",
    "IMessageSendObservation",
    "IMessageSender",
    "OsaScriptSender",
    "SQLiteMessageReader",
]
