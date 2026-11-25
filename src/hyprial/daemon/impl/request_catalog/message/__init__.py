"""Message request declarations shared by the registry and package exports."""

from .batch import PENDING_BATCH_BINDING, PENDING_BATCH_SPEC, pending_batch
from .history import MESSAGE_HISTORY_BINDING, MESSAGE_HISTORY_SPEC, message_history
from .query import (
    MESSAGE_QUERY_BINDING,
    MESSAGE_QUERY_SPEC,
    message_query,
)
from .status import MESSAGE_STATUS_BINDING, MESSAGE_STATUS_SPEC, message_status

__all__ = [
    "MESSAGE_STATUS_BINDING",
    "MESSAGE_HISTORY_BINDING",
    "MESSAGE_HISTORY_SPEC",
    "MESSAGE_QUERY_BINDING",
    "MESSAGE_QUERY_SPEC",
    "MESSAGE_STATUS_SPEC",
    "PENDING_BATCH_BINDING",
    "PENDING_BATCH_SPEC",
    "message_status",
    "message_history",
    "message_query",
    "pending_batch",
]
