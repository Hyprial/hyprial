"""Composition of the conversation recorder and body resolver (S6)."""

from hyprial.daemon.impl.composition.conversations.placement import OwnerHomePlacement
from hyprial.daemon.impl.composition.conversations.wiring import (
    RECORDER_CLOSE_TIMEOUT_S,
    RECORDER_ENV,
    close_conversation_recorder,
    compose_conversation_recorder,
    default_retention,
)

__all__ = [
    "RECORDER_CLOSE_TIMEOUT_S",
    "RECORDER_ENV",
    "OwnerHomePlacement",
    "close_conversation_recorder",
    "compose_conversation_recorder",
    "default_retention",
]
