from __future__ import annotations

import asyncio
import hashlib
import threading

from hyprial.daemon.impl.adapters.contracts import AdapterInstanceRef
from hyprial.daemon.impl.adapters.mail import (
    MailConnectionConfig,
    MailCredentials,
    MailStateStore,
    MailSmtpTransport,
    SmtpDataUnknown,
    capture_uidvalidity_rollover,
    settle_smtp_delivery,
)


def _instance() -> AdapterInstanceRef:
    fingerprint = hashlib.sha256(b"mail\0mail.example\0alice@example.com").hexdigest()
    return AdapterInstanceRef(
        "mail-1", "mail", "imap-smtp", "main", "org-1", fingerprint, 3
    )


class _Credentials:
    async def resolve(self, credential_ref: str) -> MailCredentials:
        assert credential_ref == "receiver:mail"
        return MailCredentials("alice@example.com", "app-password")


class _Smtp:
    def __init__(self, *, data_result: object = (250, b"queued")) -> None:
        self.data_result = data_result
        self.calls: list[tuple[object, ...]] = []
        self.thread_ids: list[int] = []

    def ehlo(self) -> tuple[int, bytes]:
        self.thread_ids.append(threading.get_ident())
        self.calls.append(("ehlo",))
        return 250, b"ok"

    def starttls(self, *, context: object) -> tuple[int, bytes]:
        self.calls.append(("starttls", context))
        return 220, b"ready"

    def login(self, username: str, password: str) -> tuple[int, bytes]:
        self.calls.append(("login", username, password))
        return 235, b"ok"

    def mail(self, sender: str, options: tuple[str, ...]) -> tuple[int, bytes]:
        self.calls.append(("mail", sender, options))
        return 250, b"ok"

    def rcpt(self, recipient: str, options: tuple[str, ...]) -> tuple[int, bytes]:
        self.calls.append(("rcpt", recipient, options))
        return {
            "a@example.com": (250, b"ok"),
            "b@example.com": (450, b"later"),
            "c@example.com": (550, b"denied"),
        }[recipient]

    def data(self, message: bytes) -> tuple[int, bytes]:
        self.calls.append(("data", message))
        if isinstance(self.data_result, BaseException):
            raise self.data_result
        return self.data_result  # type: ignore[return-value]

    def quit(self) -> tuple[int, bytes]:
        self.calls.append(("quit",))
        return 221, b"bye"


def test_uidvalidity_rollover_is_durable_namespaced_and_never_message_id_deduped(
    tmp_path: object,
) -> None:
    path = getattr(tmp_path, "joinpath")("mail.sqlite3")
    store = MailStateStore(path)
    store.initialize_epoch(
        mailbox_scope="mailbox-1",
        folder="INBOX",
        uidvalidity=7,
        checkpoint_uid=6,
        operation_id="bootstrap",
        now_ms=1,
    )
    epoch = store.observe_uidvalidity(
        mailbox_scope="mailbox-1",
        folder="INBOX",
        uidvalidity=8,
        snapshot_highest_uid=4,
        operation_id="roll-1",
        now_ms=2,
    )
    assert epoch.previous_epoch_id is not None
    assert epoch.state == "rollover-detected"
    assert epoch.scan_next_uid == 1
    assert epoch.scan_end_uid == 4
    assert epoch.tail_next_uid == 5
    store.quarantine(
        epoch_id=epoch.epoch_id,
        uid=4,
        message_id="same-as-old",
        raw_source_ref="sealed:mail:8:4",
    )
    store.close()

    recovered = MailStateStore(path)
    try:
        recovered_epoch = recovered.load_epoch("mailbox-1", "INBOX")
        assert recovered_epoch is not None
        assert recovered_epoch.epoch_id == epoch.epoch_id
        assert recovered_epoch.previous_epoch_id == epoch.previous_epoch_id
        assert recovered_epoch.quarantine_count == 1
        row = recovered.quarantine_row(epoch.epoch_id, 4)
        assert row == {
            "uid": 4,
            "messageId": "same-as-old",
            "decision": None,
            "rawSourceRef": "sealed:mail:8:4",
        }
    finally:
        recovered.close()
    assert capture_uidvalidity_rollover(
        {
            "mailbox": "INBOX",
            "oldUidValidity": 7,
            "newUidValidity": 8,
            "uid": 4,
            "messageId": "same",
        }
    ) == {
        "namespace": "INBOX:8",
        "quarantined": True,
        "checkpoint": 4,
        "deduplicatedByMessageId": False,
    }


def test_smtp_uses_one_data_and_keeps_partial_recipient_outcomes_without_resend() -> None:
    async def scenario() -> None:
        caller_thread = threading.get_ident()
        smtp = _Smtp(data_result=SmtpDataUnknown("connection lost after DATA"))
        config = MailConnectionConfig(
            mailbox_scope="mailbox-1",
            authenticated_mailbox_id="alice@example.com",
            imap_host="imap.example.com",
            imap_port=993,
            smtp_host="smtp.example.com",
            smtp_port=587,
            credential_ref="receiver:mail",
            smtp_mode="starttls",
        )
        transport = MailSmtpTransport(config, _Credentials(), lambda *_args, **_kwargs: smtp)
        receipt = await transport.submit(
            transaction_id="tx-1",
            outbound_message_id="msg-1@example.com",
            payload=b"From: alice@example.com\r\n\r\nhello",
            envelope_sender="alice@example.com",
            recipients=("a@example.com", "b@example.com", "c@example.com"),
            attempt=1,
        )
        assert smtp.thread_ids and smtp.thread_ids[0] != caller_thread
        assert [call[0] for call in smtp.calls].count("data") == 1
        rows = {row.recipient_id: row for row in receipt.recipients}
        assert (rows["a@example.com"].rcpt_state, rows["a@example.com"].data_state) == (
            "accepted",
            "unknown",
        )
        assert rows["a@example.com"].definitely_not_sent is False
        assert rows["b@example.com"].rcpt_state == "refused-retryable"
        assert rows["c@example.com"].rcpt_state == "refused-final"
        settled = settle_smtp_delivery(
            {"rcpt": {"a@x": 250, "b@x": 450, "c@x": 550}, "data": "timeout"}
        )
        assert settled == {
            "accepted": [],
            "retry": ["b@x"],
            "failed": ["c@x"],
            "unknown": ["a@x"],
            "resendAccepted": False,
        }

    asyncio.run(scenario())


def test_smtp_data_4xx_and_5xx_are_definitely_not_sent_and_classified() -> None:
    assert settle_smtp_delivery(
        {"rcpt": {"a@x": 250, "b@x": 450, "c@x": 550}, "data": 450}
    ) == {
        "accepted": [],
        "retry": ["a@x", "b@x"],
        "failed": ["c@x"],
        "unknown": [],
        "resendAccepted": False,
    }
    assert settle_smtp_delivery(
        {"rcpt": {"a@x": 250, "b@x": 450, "c@x": 550}, "data": 550}
    ) == {
        "accepted": [],
        "retry": ["b@x"],
        "failed": ["a@x", "c@x"],
        "unknown": [],
        "resendAccepted": False,
    }
