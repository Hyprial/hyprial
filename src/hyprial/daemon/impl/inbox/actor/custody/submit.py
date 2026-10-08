from __future__ import annotations
import hashlib
import json
from hyprial.kernel import PortCommandRejected
from hyprial.daemon.impl.inbox.contracts.api  import (
    InboxMessage,
    SubmissionResult,
)
from hyprial.daemon.impl.inbox.contracts.ports  import (
    BoolMutationCompleted,
    MessagesMutationCompleted,
    SubmissionCompleted,
    SubmissionProjection,
    SubmitMessageCommand,
)

from ..events import (
    DispatchIoKind,
    DispatchIoRequested,
    DispatchItem,
)
from ..internal import (
    _DurableSubmissionReceipt,
    _SubmissionReceiptConflict,
)

class DeliveryCustodySubmitMixin:
    def _stage_submit(self, command: SubmitMessageCommand) -> None:
        try:
            receipt = self._read_submission_receipt(command)
        except _SubmissionReceiptConflict as error:
            self._publish(
                PortCommandRejected(
                    correlation_id=command.correlation_id,
                    domain="inbox",
                    generation=self._generation,
                    version=self._version,
                    code="SUBMISSION_RECEIPT_CONFLICT",
                    detail=str(error),
                )
            )
            return
        if receipt is not None:
            self._publish(
                SubmissionCompleted(
                    correlation_id=command.correlation_id,
                    generation=self._generation,
                    version=self._version,
                    result=receipt.result,
                )
            )
            return
        if command.correlation_id in self._pending_io:
            self._reject_correlation(command.correlation_id)
            return
        now = self._service._now_ms() if command.now_ms is None else command.now_ms
        self._service._insert_outbox(command.message, now)
        if command.defer_direct:
            version = self._committed_version()
            result = SubmissionResult(command.message.message_id, True, queued=True)
            self._persist_submission_receipt(
                command.correlation_id,
                command.message,
                result,
                now,
            )
            self._publish_submission(
                command.correlation_id,
                version,
                result,
            )
            return
        version = self._committed_version()
        request = DispatchIoRequested(
            correlation_id=command.correlation_id,
            generation=self._generation,
            version=version,
            kind=DispatchIoKind.DELIVERY,
            items=(DispatchItem(command.message),),
        )
        self._request_io(request, completion_kind="submit")
    def _publish_bool(
        self,
        correlation_id: str,
        operation: str,
        result: bool,
    ) -> None:
        version = self._committed_version()
        self._publish(
            BoolMutationCompleted(
                correlation_id,
                self._generation,
                version,
                operation,
                result,
            )
        )
    def _publish_messages(
        self,
        correlation_id: str,
        operation: str,
        messages: tuple[InboxMessage, ...],
    ) -> None:
        version = self._committed_version()
        self._publish(
            MessagesMutationCompleted(
                correlation_id,
                self._generation,
                version,
                operation,
                messages,
            )
        )
    def _publish_missing(self, correlation_id: str, message_id: str) -> None:
        self._publish(
            PortCommandRejected(
                correlation_id=correlation_id,
                domain="inbox",
                generation=self._generation,
                version=self._version,
                code="MESSAGE_NOT_FOUND",
                detail=message_id,
            )
        )
    def _create_submission_receipts(self) -> None:
        """D22: durable effect-level settlement owned by the inbox database."""

        with self._service._db:
            self._service._db.execute(
                """CREATE TABLE IF NOT EXISTS actor_submission_receipts (
                       correlation_id TEXT PRIMARY KEY,
                       command_digest TEXT NOT NULL,
                       message_id TEXT NOT NULL,
                       accepted INTEGER NOT NULL,
                       queued INTEGER NOT NULL,
                       code TEXT,
                       custody_mailbox TEXT,
                       recorded_at_ms INTEGER NOT NULL
                   )"""
            )
    def _read_submission_receipt(
        self, command: SubmitMessageCommand
    ) -> _DurableSubmissionReceipt | None:
        row = self._service._db.execute(
            "SELECT * FROM actor_submission_receipts WHERE correlation_id = ?",
            (command.correlation_id,),
        ).fetchone()
        if row is None:
            return None
        digest = self._submission_command_digest(command.message)
        if str(row["command_digest"]) != digest:
            raise _SubmissionReceiptConflict(
                "correlation_id is already settled for a different inbox command"
            )
        return _DurableSubmissionReceipt(
            command_digest=digest,
            result=SubmissionProjection(
                message_id=str(row["message_id"]),
                accepted=bool(row["accepted"]),
                queued=bool(row["queued"]),
                code=(str(row["code"]) if row["code"] is not None else None),
                custody_mailbox=(
                    str(row["custody_mailbox"])
                    if row["custody_mailbox"] is not None
                    else None
                ),
            ),
        )
    def _advance_reply_receipt_locked(
        self,
        message: InboxMessage,
        result: SubmissionResult,
        now_ms: int,
    ) -> None:
        """D22: the background outbox settles the receipt its submit left queued.

        Once the single native I/O reaches a terminal outcome the queued
        durable receipt advances to delivered/failed under the same stable
        correlation, so a caller retry joins that terminal instead of holding
        a queued receipt forever.  Settled (non-queued) receipts are never
        rewritten, and the command digest keeps the advance bound to the same
        inbox command.
        """

        if result.queued:
            return
        correlation = self._stable_reply_correlation(message)
        if correlation is None:
            return
        self._service._db.execute(
            """UPDATE actor_submission_receipts
                  SET accepted = ?, queued = 0, code = ?, custody_mailbox = ?,
                      recorded_at_ms = ?
                WHERE correlation_id = ? AND command_digest = ?
                  AND message_id = ? AND queued = 1""",
            (
                int(result.accepted),
                result.code,
                result.custody_mailbox,
                now_ms,
                correlation,
                self._submission_command_digest(message),
                result.message_id,
            ),
        )
        if result.accepted and isinstance(message.idempotency_key, str):
            inbound_message_id = message.idempotency_key.removeprefix("reply:")
            inbound = self._service._db.execute(
                "SELECT recipient FROM inbox WHERE message_id = ?",
                (inbound_message_id,),
            ).fetchone()
            if inbound is not None:
                self._service.ack(str(inbound["recipient"]), inbound_message_id)
    def _settle_submission_receipt_locked(
        self,
        receipt_correlation_id: str | None,
        message: InboxMessage,
        result: SubmissionResult,
        now_ms: int,
    ) -> None:
        if receipt_correlation_id is not None:
            self._persist_submission_receipt_locked(
                receipt_correlation_id,
                message,
                result,
                now_ms,
            )
            return
        self._advance_reply_receipt_locked(message, result, now_ms)
    def _persist_submission_receipt(
        self,
        correlation_id: str,
        message: InboxMessage,
        result: SubmissionResult,
        now_ms: int,
    ) -> None:
        with self._service._db:
            self._persist_submission_receipt_locked(
                correlation_id,
                message,
                result,
                now_ms,
            )
    def _persist_submission_receipt_locked(
        self,
        correlation_id: str,
        message: InboxMessage,
        result: SubmissionResult,
        now_ms: int,
    ) -> None:
        digest = self._submission_command_digest(message)
        self._service._db.execute(
            """INSERT OR IGNORE INTO actor_submission_receipts
                   (correlation_id, command_digest, message_id, accepted, queued,
                    code, custody_mailbox, recorded_at_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                correlation_id,
                digest,
                result.message_id,
                int(result.accepted),
                int(result.queued),
                result.code,
                result.custody_mailbox,
                now_ms,
            ),
        )
        row = self._service._db.execute(
            "SELECT command_digest, message_id FROM actor_submission_receipts "
            "WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
        if (
            row is None
            or str(row["command_digest"]) != digest
            or str(row["message_id"]) != result.message_id
        ):
            raise _SubmissionReceiptConflict(
                "correlation_id is already settled for a different inbox command"
            )
    @staticmethod
    def _submission_command_digest(message: InboxMessage) -> str:
        body = json.dumps(
            {
                "conversation_id": message.conversation_id,
                "sender": message.sender,
                "recipient": message.recipient,
                "payload": message.payload.hex(),
                "intent": message.intent,
                "lifecycle": message.lifecycle.value,
                "idempotency_key": message.idempotency_key,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return "sha256:" + hashlib.sha256(body).hexdigest()
