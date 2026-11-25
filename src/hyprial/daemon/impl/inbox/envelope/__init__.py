"""Message envelope: class rules, inheritance, storage columns and the payload inner envelope."""

from hyprial.daemon.impl.inbox.envelope.columns import (
    ENVELOPE_COLUMN_LIST,
    ENVELOPE_COLUMNS,
    ENVELOPE_PLACEHOLDERS,
    ENVELOPE_TABLES,
    ensure_envelope_columns,
    envelope_fields,
    envelope_values,
    stored_message,
)
from hyprial.daemon.impl.inbox.envelope.payload import (
    PAYLOAD_ENVELOPE_KEY,
    PAYLOAD_ENVELOPE_VERSION,
    STAMPED_INTENTS,
    adopt_payload_envelope,
    payload_envelope,
    split_payload,
    stamp_payload,
)
from hyprial.daemon.impl.inbox.envelope.rules import (
    SYSTEM_INTENT,
    SYSTEM_SENDER_PREFIX,
    derive_message_class,
    inherit_envelope,
    message_class_of,
)
from hyprial.daemon.impl.inbox.envelope.transport import EnvelopeStampingTransport

__all__ = [
    "ENVELOPE_COLUMNS",
    "ENVELOPE_COLUMN_LIST",
    "ENVELOPE_PLACEHOLDERS",
    "ENVELOPE_TABLES",
    "EnvelopeStampingTransport",
    "PAYLOAD_ENVELOPE_KEY",
    "PAYLOAD_ENVELOPE_VERSION",
    "STAMPED_INTENTS",
    "SYSTEM_INTENT",
    "SYSTEM_SENDER_PREFIX",
    "adopt_payload_envelope",
    "derive_message_class",
    "ensure_envelope_columns",
    "envelope_fields",
    "envelope_values",
    "inherit_envelope",
    "message_class_of",
    "payload_envelope",
    "split_payload",
    "stamp_payload",
    "stored_message",
]
