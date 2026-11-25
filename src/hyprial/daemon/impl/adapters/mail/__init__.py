"""Standard-library IMAP+SMTP adapter implementation."""

from .mime import MailMessageCodec, address_key
from .models import (
    MailCheckpoint,
    MailCheckpointLedger,
    MailConnectionConfig,
    MailCredentialResolver,
    MailCredentials,
    MailFetchEvidence,
)
from .receipts import MAX_RECEIPT_BYTES, MailReceiptProcessor
from .runtime import MailImapRuntime, MailRuntimeBlockedError
from .smtp import MailSmtpTransport, SmtpDataUnknown, settle_smtp_delivery
from .state import MailStateStore, capture_uidvalidity_rollover

__all__ = [
    "MailCheckpoint",
    "MailCheckpointLedger",
    "MailConnectionConfig",
    "MailCredentialResolver",
    "MailCredentials",
    "MailFetchEvidence",
    "MailImapRuntime",
    "MailMessageCodec",
    "MailReceiptProcessor",
    "MailRuntimeBlockedError",
    "MailSmtpTransport",
    "MailStateStore",
    "SmtpDataUnknown",
    "MAX_RECEIPT_BYTES",
    "address_key",
    "capture_uidvalidity_rollover",
    "settle_smtp_delivery",
]
