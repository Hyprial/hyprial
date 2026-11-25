from __future__ import annotations

import asyncio
import hashlib
import threading
from email.message import EmailMessage
from types import SimpleNamespace

from hyprial.daemon.impl.adapters.contracts import AdapterInstanceRef, CustodyReceipt
from hyprial.daemon.impl.adapters.mail import (
    MailCheckpoint,
    MailConnectionConfig,
    MailCredentials,
    MailFetchEvidence,
    MailImapRuntime,
    MailMessageCodec,
)


def _instance() -> AdapterInstanceRef:
    fingerprint = hashlib.sha256(b"mail\0mail.example\0alice@example.com").hexdigest()
    return AdapterInstanceRef(
        "mail-1", "mail", "imap-smtp", "main", "org-1", fingerprint, 3
    )


class _Credentials:
    async def resolve(self, credential_ref: str) -> MailCredentials:
        return MailCredentials("alice@example.com", "app-password")


class _Ledger:
    def __init__(self, value: MailCheckpoint | None = None) -> None:
        self.value = value

    async def load(self, instance_id: str, generation: int, folder: str) -> MailCheckpoint | None:
        return self.value

    async def commit(
        self, instance_id: str, generation: int, folder: str, checkpoint: MailCheckpoint
    ) -> None:
        self.value = checkpoint


class _Imap:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.thread_ids: list[int] = []
        self.closed = False

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        self.thread_ids.append(threading.get_ident())
        return "OK", [b"logged in"]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        assert readonly is True
        return "OK", [b"1"]

    def response(self, name: str) -> tuple[str, list[bytes]]:
        return "UIDVALIDITY", [b"7"]

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        if command == "search":
            assert args[-1] == "UID 1:*"
            return "OK", [b"4"]
        assert command == "fetch"
        return "OK", [(b"4 (RFC822 {10})", self.raw), b")"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.closed = True
        return "BYE", [b"bye"]


def _message() -> bytes:
    message = EmailMessage()
    message["From"] = "Ada <ada@example.com>"
    message["To"] = "Alice <alice@example.com>, Bob <bob@example.com>"
    message["Cc"] = "Carol <carol@example.com>"
    message["Bcc"] = "Hidden <hidden@example.com>"
    message["Message-ID"] = "<m-1@example.com>"
    message["In-Reply-To"] = "<prior@example.com>"
    message["References"] = "<root@example.com> <prior@example.com>"
    message["Subject"] = "Re: status"
    message.set_content("plain body")
    message.add_alternative("<p>rich body</p>", subtype="html")
    message.add_attachment(b"report", maintype="application", subtype="pdf", filename="r.pdf")
    return message.as_bytes()


def test_mime_normalization_preserves_reply_html_attachment_and_hides_other_bcc() -> None:
    raw = _message()
    normalized, platform = MailMessageCodec(
        _instance(),
        MailConnectionConfig(
            mailbox_scope="mailbox-1",
            authenticated_mailbox_id="alice@example.com",
            imap_host="imap.example.com",
            imap_port=993,
            smtp_host="smtp.example.com",
            smtp_port=465,
            credential_ref="receiver:mail",
            smtp_mode="implicit-tls",
        ),
        self_addresses=("alice@example.com",),
    ).decode_message(
        raw,
        fetch_evidence=MailFetchEvidence(
            "mailbox-1",
            "alice@example.com",
            "INBOX",
            7,
            4,
            hashlib.sha256(raw).hexdigest(),
        ),
        raw_source_ref="sealed:mail:7:4",
        received_at_ms=10,
    )
    assert platform.envelope.bcc_visibility == "none"
    visible = tuple(row.address.address for row in platform.envelope.recipients)
    assert visible == ("alice@example.com", "bob@example.com", "carol@example.com")
    assert "hidden@example.com" not in visible
    assert normalized.reply_to is not None
    assert normalized.reply_to.source_native_message_id == "prior@example.com"
    assert normalized.context.channel_id.startswith("adapter:mail:")
    assert {handle.name for handle in normalized.attachments} == {"r.pdf"}
    assert [block.type for block in normalized.content] == ["text", "rich-text"]


def test_imap_runtime_uses_worker_thread_and_commits_cursor_only_after_custody() -> None:
    async def scenario() -> None:
        caller_thread = threading.get_ident()
        imap = _Imap(_message())
        config = MailConnectionConfig(
            mailbox_scope="mailbox-1",
            authenticated_mailbox_id="alice@example.com",
            imap_host="imap.example.com",
            imap_port=993,
            smtp_host="smtp.example.com",
            smtp_port=465,
            credential_ref="receiver:mail",
            smtp_mode="implicit-tls",
        )
        ledger = _Ledger()
        runtime = MailImapRuntime(config, _Credentials(), ledger, lambda *_a, **_k: imap)
        session = await runtime.start(_instance(), SimpleNamespace())
        assert session.ingress_mode == "pull"
        unit = await runtime.next_ingress()
        assert imap.thread_ids and imap.thread_ids[0] != caller_thread
        assert unit.checkpoint.token == "mailbox-1:INBOX:7:4"
        assert ledger.value is None
        await runtime.commit_checkpoint(
            unit.checkpoint,
            CustodyReceipt("journal-1", unit.envelope.observation.observation_digest, 12),
        )
        assert ledger.value == MailCheckpoint("mailbox-1", "INBOX", 7, 4)
        await runtime.close(1000)
        assert imap.closed is True

    asyncio.run(scenario())
