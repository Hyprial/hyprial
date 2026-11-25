"""Bounded, authenticated DSN/MDN parsing and durable reconciliation."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser

from ..contracts import MailDeliveryObservation
from .state import MailStateStore


MAX_RECEIPT_BYTES = 256 * 1024
_MAX_MIME_PARTS = 32


@dataclass(frozen=True, slots=True)
class _ParsedReceipt:
    transaction_id: str | None
    outbound_message_id: str | None
    recipient: str
    kind: str
    delivery_state: str


def _identifier(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.startswith("<") and normalized.endswith(">"):
        normalized = normalized[1:-1]
    return normalized or None


def _recipient(value: str | None) -> str:
    if not value or ";" not in value:
        raise ValueError("mail receipt Final-Recipient is missing")
    kind, address = value.split(";", 1)
    if kind.strip().casefold() != "rfc822" or not address.strip():
        raise ValueError("mail receipt Final-Recipient is unsupported")
    return address.strip().casefold()


def _report_blocks(part: Message) -> tuple[Message, ...]:
    payload = part.get_payload()
    if not isinstance(payload, list) or not all(isinstance(item, Message) for item in payload):
        raise ValueError("mail receipt report block is malformed")
    return tuple(payload)


def _parse_receipt(message: Message) -> _ParsedReceipt:
    if message.get_content_type() != "multipart/report":
        raise ValueError("mail receipt must be multipart/report")
    parts = tuple(message.walk())
    if len(parts) > _MAX_MIME_PARTS:
        raise ValueError("mail receipt has too many MIME parts")
    report_type = str(message.get_param("report-type") or "").casefold()
    if report_type == "delivery-status":
        reports = [part for part in parts if part.get_content_type() == "message/delivery-status"]
        if len(reports) != 1:
            raise ValueError("mail DSN requires one delivery-status report")
        blocks = _report_blocks(reports[0])
        if len(blocks) != 2:
            raise ValueError("mail DSN v1 requires one recipient result")
        transaction_id = _identifier(blocks[0].get("Original-Envelope-Id"))
        outbound = _identifier(blocks[0].get("Original-Message-ID"))
        recipient = _recipient(blocks[1].get("Final-Recipient"))
        action = str(blocks[1].get("Action") or "").casefold()
        mapped = {
            "delivered": ("dsn-delivered", "delivered"),
            "delayed": ("dsn-delayed", "delayed"),
            "failed": ("dsn-failed", "failed"),
        }.get(action)
        if mapped is None:
            raise ValueError("mail DSN action is unsupported")
        return _ParsedReceipt(transaction_id, outbound, recipient, *mapped)
    if report_type == "disposition-notification":
        reports = [
            part
            for part in parts
            if part.get_content_type() == "message/disposition-notification"
        ]
        if len(reports) != 1:
            raise ValueError("mail MDN requires one disposition report")
        blocks = _report_blocks(reports[0])
        if len(blocks) != 1:
            raise ValueError("mail MDN v1 requires one recipient result")
        disposition = str(blocks[0].get("Disposition") or "").casefold()
        if not disposition.endswith("; displayed"):
            raise ValueError("mail MDN disposition is unsupported")
        return _ParsedReceipt(
            None,
            _identifier(blocks[0].get("Original-Message-ID")),
            _recipient(blocks[0].get("Final-Recipient")),
            "mdn-displayed",
            "displayed",
        )
    raise ValueError("mail receipt report type is unsupported")


class MailReceiptProcessor:
    """Stage receipt bytes before parsing, then reconcile from durable state."""

    def __init__(
        self, store: MailStateStore, *, authenticator: Callable[[Message], bool]
    ) -> None:
        self._store = store
        self._authenticator = authenticator

    def stage(self, raw: bytes, *, source_message_id: str, observed_at_ms: int) -> str:
        if not isinstance(raw, bytes) or not raw:
            raise ValueError("mail receipt bytes are required")
        if len(raw) > MAX_RECEIPT_BYTES:
            raise ValueError("mail receipt size exceeds the bounded limit")
        return self._store.stage_receipt(
            bytes(raw), source_message_id=source_message_id, observed_at_ms=observed_at_ms
        )

    def reconcile_pending(self, *, limit: int) -> tuple[str, ...]:
        settled: list[str] = []
        for row in self._store.pending_receipts(limit=limit):
            receipt_id = str(row["receipt_id"])
            raw = bytes(row["raw_bytes"])
            try:
                message = BytesParser(policy=policy.default).parsebytes(raw)
                if not self._authenticator(message):
                    self._store.settle_receipt(
                        receipt_id=receipt_id,
                        observation=None,
                        delivery_state=None,
                        reason_code="MAIL_RECEIPT_AUTHENTICATION_FAILED",
                    )
                    settled.append(receipt_id)
                    continue
                parsed = _parse_receipt(message)
                target = self._store.lookup_delivery(
                    transaction_id=parsed.transaction_id,
                    outbound_message_id=parsed.outbound_message_id,
                    address=parsed.recipient,
                )
                if target is None:
                    self._store.settle_receipt(
                        receipt_id=receipt_id,
                        observation=None,
                        delivery_state=None,
                        reason_code="MAIL_RECEIPT_TRANSACTION_NOT_FOUND",
                    )
                    settled.append(receipt_id)
                    continue
                transaction_id, recipient_id = target
                digest = str(row["evidence_digest"])
                observation = MailDeliveryObservation(
                    hashlib.sha256(
                        f"{parsed.kind}\0{transaction_id}\0{recipient_id}\0{digest}".encode()
                    ).hexdigest(),
                    transaction_id,
                    recipient_id,
                    parsed.kind,  # type: ignore[arg-type]
                    True,
                    str(row["source_message_id"]),
                    int(row["observed_at_ms"]),
                    digest,
                )
                self._store.settle_receipt(
                    receipt_id=receipt_id,
                    observation=observation,
                    delivery_state=parsed.delivery_state,
                    reason_code="MAIL_RECEIPT_RECONCILED",
                )
            except (TypeError, ValueError):
                self._store.settle_receipt(
                    receipt_id=receipt_id,
                    observation=None,
                    delivery_state=None,
                    reason_code="MAIL_RECEIPT_MALFORMED",
                )
            settled.append(receipt_id)
        return tuple(settled)


__all__ = ["MAX_RECEIPT_BYTES", "MailReceiptProcessor"]
