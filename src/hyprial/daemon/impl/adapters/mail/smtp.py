"""One-DATA SMTP transport with per-recipient durable outcomes."""

from __future__ import annotations

import asyncio
import hashlib
import re
import smtplib
import ssl
from collections.abc import Callable, Mapping, Sequence

from ..contracts import SmtpRecipientSubmission, SmtpTransactionReceipt
from .models import MailConnectionConfig, MailCredentialResolver, MailCredentials
from .state import MailStateStore


class SmtpDataUnknown(ConnectionError):
    """Raised only after DATA submission started and no complete reply was received."""


_ENHANCED_STATUS = re.compile(rb"\b([245]\.\d\.\d{1,3})\b")


def _status(code: int, response: bytes) -> str | None:
    match = _ENHANCED_STATUS.search(response)
    return match.group(1).decode("ascii") if match else None


def _default_factory(config: MailConnectionConfig, context: ssl.SSLContext) -> object:
    if config.smtp_mode == "implicit-tls":
        return smtplib.SMTP_SSL(
            config.smtp_host, config.smtp_port, timeout=30, context=context
        )
    return smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)


class MailSmtpTransport:
    """Run stdlib SMTP blocking I/O in a dedicated asyncio worker thread."""

    def __init__(
        self,
        config: MailConnectionConfig,
        credentials: MailCredentialResolver,
        factory: Callable[..., object] | None = None,
        *,
        state_store: MailStateStore | None = None,
    ) -> None:
        self._config = config
        self._credentials = credentials
        self._context = ssl.create_default_context()
        self._factory = factory
        self._state_store = state_store

    async def submit(
        self,
        *,
        transaction_id: str,
        outbound_message_id: str,
        payload: bytes,
        envelope_sender: str,
        recipients: Sequence[str],
        attempt: int,
    ) -> SmtpTransactionReceipt:
        credentials = await self._credentials.resolve(self._config.credential_ref)
        receipt = await asyncio.to_thread(
            self._submit_sync,
            credentials,
            transaction_id,
            outbound_message_id,
            bytes(payload),
            envelope_sender,
            tuple(recipients),
            attempt,
        )
        if self._state_store is not None:
            self._state_store.record_smtp_receipt(receipt)
        return receipt

    def _submit_sync(
        self,
        credentials: MailCredentials,
        transaction_id: str,
        outbound_message_id: str,
        payload: bytes,
        envelope_sender: str,
        recipients: tuple[str, ...],
        attempt: int,
    ) -> SmtpTransactionReceipt:
        if not recipients or len(recipients) != len(set(recipients)):
            raise ValueError("SMTP recipients must be non-empty and unique")
        client = (
            self._factory(self._config.smtp_host, self._config.smtp_port, context=self._context)
            if self._factory
            else _default_factory(self._config, self._context)
        )
        rcpt_results: dict[str, tuple[int, bytes]] = {}
        data_result: tuple[int | None, bytes] | BaseException = (None, b"")
        try:
            client.ehlo()
            if self._config.smtp_mode == "starttls":
                client.starttls(context=self._context)
                client.ehlo()
            client.login(credentials.username, credentials.password)
            mail_code, mail_response = client.mail(envelope_sender, ("BODY=8BITMIME",))
            if mail_code // 100 != 2:
                raise smtplib.SMTPSenderRefused(mail_code, mail_response, envelope_sender)
            for recipient in recipients:
                code, response = client.rcpt(
                    recipient, ("NOTIFY=SUCCESS,FAILURE,DELAY",)
                )
                rcpt_results[recipient] = (int(code), bytes(response))
            accepted = [item for item, (code, _response) in rcpt_results.items() if code // 100 == 2]
            if accepted:
                try:
                    code, response = client.data(payload)
                    data_result = (int(code), bytes(response))
                except (OSError, EOFError, smtplib.SMTPServerDisconnected, SmtpDataUnknown) as exc:
                    data_result = exc
        finally:
            try:
                client.quit()
            except (OSError, EOFError, smtplib.SMTPException):
                pass
        rows = tuple(
            self._submission(recipient, attempt, *rcpt_results[recipient], data_result)
            for recipient in recipients
        )
        return SmtpTransactionReceipt(
            transaction_id,
            outbound_message_id,
            hashlib.sha256(payload).hexdigest(),
            rows,
        )

    @staticmethod
    def _submission(
        recipient: str,
        attempt: int,
        rcpt_code: int,
        rcpt_response: bytes,
        data_result: tuple[int | None, bytes] | BaseException,
    ) -> SmtpRecipientSubmission:
        if rcpt_code // 100 == 4:
            return SmtpRecipientSubmission(
                recipient, attempt, "refused-retryable", "not-attempted", True,
                rcpt_code, _status(rcpt_code, rcpt_response), "SMTP_RCPT_RETRYABLE",
            )
        if rcpt_code // 100 != 2:
            return SmtpRecipientSubmission(
                recipient, attempt, "refused-final", "not-attempted", True,
                rcpt_code, _status(rcpt_code, rcpt_response), "SMTP_RCPT_REFUSED",
            )
        if isinstance(data_result, BaseException) or data_result[0] is None:
            return SmtpRecipientSubmission(
                recipient, attempt, "accepted", "unknown", False, None, None,
                "SMTP_DATA_OUTCOME_UNKNOWN",
            )
        code, response = data_result
        if code // 100 == 2:
            state, definitely, reason = "accepted", False, None
        elif code // 100 == 4:
            state, definitely, reason = "rejected-retryable", True, "SMTP_DATA_RETRYABLE"
        else:
            state, definitely, reason = "rejected-final", True, "SMTP_DATA_REFUSED"
        return SmtpRecipientSubmission(
            recipient, attempt, "accepted", state, definitely, code,
            _status(code, response), reason,
        )


def settle_smtp_delivery(value: Mapping[str, object]) -> dict[str, object]:
    """Return the frozen common-settlement projection without hiding overlaps."""

    rcpt_raw = value.get("rcpt")
    if not isinstance(rcpt_raw, Mapping):
        raise ValueError("rcpt must be a mapping")
    recipients = sorted((str(key), int(code)) for key, code in rcpt_raw.items())
    data = value.get("data")
    accepted_rcpt = [recipient for recipient, code in recipients if code // 100 == 2]
    retry = [recipient for recipient, code in recipients if code // 100 == 4]
    failed = [recipient for recipient, code in recipients if code // 100 == 5]
    unknown: list[str] = []
    accepted: list[str] = []
    if data == "timeout":
        unknown.extend(accepted_rcpt)
    elif isinstance(data, int) and data // 100 == 4:
        retry.extend(accepted_rcpt)
    elif isinstance(data, int) and data // 100 == 5:
        failed.extend(accepted_rcpt)
    elif isinstance(data, int) and data // 100 == 2:
        accepted.extend(accepted_rcpt)
    else:
        raise ValueError("unsupported SMTP DATA result")
    return {
        "accepted": sorted(accepted),
        "retry": sorted(retry),
        "failed": sorted(failed),
        "unknown": sorted(unknown),
        "resendAccepted": False,
    }


__all__ = ["MailSmtpTransport", "SmtpDataUnknown", "settle_smtp_delivery"]
