from __future__ import annotations
from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.inbox.actor.errors  import InboxAuthorityTimeout
from hyprial.daemon.impl.inbox.contracts.api  import (
    InboxMessage,
)

class InboxSubmissionOutcomeUnknown(InboxAuthorityTimeout):
    """Mailbox admission succeeded; durable commit/delivery remains unknown."""

    code = ipc_errors.SUBMIT_OUTCOME_UNKNOWN

    def __init__(self, message: InboxMessage, correlation: str) -> None:
        super().__init__("submitted inbox command has no confirmed outcome")
        self.data = {
            "commandAdmission": "accepted",
            "outcomeKnown": False,
            "messageId": message.message_id,
            "submissionCorrelationId": correlation,
            "idempotencyKey": message.idempotency_key,
        }
