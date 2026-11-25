from __future__ import annotations

import asyncio
import hashlib
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters import mail
from hyprial.daemon.impl.adapters.contracts import AdapterInstanceRef, CustodyReceipt


def _instance(*, instance_id: str = "mail-1", generation: int = 3) -> AdapterInstanceRef:
    fingerprint = hashlib.sha256(
        f"mail\0mail.example\0{instance_id}".encode()
    ).hexdigest()
    return AdapterInstanceRef(
        instance_id, "mail", "imap-smtp", "main", "org-1", fingerprint, generation
    )


class _Credentials:
    async def resolve(self, credential_ref: str) -> mail.MailCredentials:
        assert credential_ref == "receiver:mail"
        return mail.MailCredentials("alice@example.com", "app-password")


class _Ledger:
    def __init__(self, checkpoint: mail.MailCheckpoint | None = None) -> None:
        self.checkpoint = checkpoint

    async def load(
        self, instance_id: str, generation: int, folder: str
    ) -> mail.MailCheckpoint | None:
        return self.checkpoint

    async def commit(
        self,
        instance_id: str,
        generation: int,
        folder: str,
        checkpoint: mail.MailCheckpoint,
    ) -> None:
        self.checkpoint = checkpoint


class _Imap:
    def __init__(self, *, uidvalidity: int = 7, uids: tuple[int, ...] = (4, 5)) -> None:
        self.uidvalidity = uidvalidity
        self.uids = uids

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        return "OK", [b"logged in"]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        assert readonly is True
        return "OK", [str(len(self.uids)).encode()]

    def response(self, name: str) -> tuple[str, list[bytes]]:
        assert name == "UIDVALIDITY"
        return "UIDVALIDITY", [str(self.uidvalidity).encode()]

    def status(self, folder: str, query: str) -> tuple[str, list[bytes]]:
        assert query == "(UIDNEXT)"
        return "OK", [f"{folder} (UIDNEXT {max(self.uids, default=0) + 1})".encode()]

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        if command == "search":
            start = int(str(args[-1]).removeprefix("UID ").removesuffix(":*"))
            return "OK", [b" ".join(str(uid).encode() for uid in self.uids if uid >= start)]
        uid = int(str(args[0]))
        raw = _message(message_id=f"m-{uid}@example.com")
        return "OK", [(b"message", raw), b")"]

    def logout(self) -> tuple[str, list[bytes]]:
        return "BYE", [b"bye"]


def _config(
    *,
    scope: str = "mailbox-1",
    folder: str = "INBOX",
    authenticated_mailbox_id: str = "alice@example.com",
) -> mail.MailConnectionConfig:
    return mail.MailConnectionConfig(
        mailbox_scope=scope,
        authenticated_mailbox_id=authenticated_mailbox_id,
        imap_host="imap.example.com",
        imap_port=993,
        smtp_host="smtp.example.com",
        smtp_port=465,
        credential_ref="receiver:mail",
        smtp_mode="implicit-tls",
        folder=folder,
        page_size=2,
    )


def _message(*, message_id: str = "m-1@example.com", visible_self: bool = False) -> bytes:
    message = EmailMessage()
    message["From"] = "Ada <ada@example.com>"
    message["To"] = (
        "Alice <alice@example.com>, Bob <bob@example.com>"
        if visible_self
        else "Bob <bob@example.com>"
    )
    message["Bcc"] = "Mallory <mallory@example.com>"
    message["Message-ID"] = f"<{message_id}>"
    message.set_content("hello")
    return message.as_bytes()


def test_checkpoint_requires_the_exact_pending_observation_and_custody_digest() -> None:
    async def scenario() -> None:
        runtime = mail.MailImapRuntime(
            _config(), _Credentials(), _Ledger(), lambda *_a, **_k: _Imap()
        )
        await runtime.start(_instance(), SimpleNamespace())
        first = await runtime.next_ingress()
        second = await runtime.next_ingress()

        with pytest.raises(ValueError, match="custody"):
            await runtime.commit_checkpoint(
                first.checkpoint,
                CustodyReceipt(
                    "journal-for-second",
                    second.envelope.observation.observation_digest,
                    10,
                ),
            )
        with pytest.raises(ValueError, match="custody"):
            await runtime.commit_checkpoint(
                first.checkpoint, CustodyReceipt("forged", "forged-digest", 11)
            )
        await runtime.commit_checkpoint(
            first.checkpoint,
            CustodyReceipt(
                "journal-for-first",
                first.envelope.observation.observation_digest,
                12,
            ),
        )

    asyncio.run(scenario())


def test_checkpoint_scope_includes_account_folder_instance_and_generation() -> None:
    async def scenario() -> None:
        foreign = mail.MailCheckpoint("other-account", "Archive", 7, 4)
        runtime = mail.MailImapRuntime(
            _config(), _Credentials(), _Ledger(foreign), lambda *_a, **_k: _Imap()
        )
        with pytest.raises(mail.MailRuntimeBlockedError, match="CHECKPOINT_SCOPE"):
            await runtime.start(_instance(), SimpleNamespace())

    asyncio.run(scenario())


def test_uidvalidity_rollover_pages_quarantine_and_reopens_from_durable_state(
    tmp_path: object,
) -> None:
    async def scenario() -> None:
        path = getattr(tmp_path, "joinpath")("mail-state.sqlite3")
        store = mail.MailStateStore(path)
        store.initialize_epoch(
            mailbox_scope="mailbox-1",
            folder="INBOX",
            uidvalidity=7,
            checkpoint_uid=9,
            operation_id="bootstrap",
            now_ms=1,
        )
        try:
            runtime = mail.MailImapRuntime(
                _config(),
                _Credentials(),
                _Ledger(),
                lambda *_a, **_k: _Imap(uidvalidity=8, uids=(1, 2, 3)),
                state_store=store,
            )
            await runtime.start(_instance(), SimpleNamespace())
            first = await runtime.next_ingress()
            assert dict(first.envelope.payload)["rollover_quarantined"] is True
            await runtime.commit_checkpoint(
                first.checkpoint,
                CustodyReceipt(
                    "journal-1", first.envelope.observation.observation_digest, 2
                ),
            )
        finally:
            store.close()

        reopened_store = mail.MailStateStore(path)
        try:
            reopened = mail.MailImapRuntime(
                _config(),
                _Credentials(),
                _Ledger(mail.MailCheckpoint("mailbox-1", "INBOX", 7, 9)),
                lambda *_a, **_k: _Imap(uidvalidity=8, uids=(1, 2, 3)),
                state_store=reopened_store,
            )
            await reopened.start(_instance(), SimpleNamespace())
            resumed = await reopened.next_ingress()
            assert resumed.checkpoint.token is not None
            assert mail.MailCheckpoint.parse(resumed.checkpoint.token).uid == 2
            epoch = reopened_store.load_epoch("mailbox-1", "INBOX")
            assert epoch is not None
            assert epoch.uidvalidity == 8
            assert epoch.scan_next_uid == 2
            assert reopened_store.quarantine_row(epoch.epoch_id, 1) is not None
        finally:
            reopened_store.close()

    asyncio.run(scenario())


def test_same_epoch_custody_commit_reopens_after_the_committed_uid(tmp_path: object) -> None:
    async def scenario() -> None:
        path = getattr(tmp_path, "joinpath")("mail-state.sqlite3")
        ledger = _Ledger()
        store = mail.MailStateStore(path)
        runtime = mail.MailImapRuntime(
            _config(),
            _Credentials(),
            ledger,
            lambda *_a, **_k: _Imap(uidvalidity=7, uids=(4, 5)),
            state_store=store,
        )
        await runtime.start(_instance(), SimpleNamespace())
        first = await runtime.next_ingress()
        assert mail.MailCheckpoint.parse(first.checkpoint.token or "").uid == 4
        await runtime.commit_checkpoint(
            first.checkpoint,
            CustodyReceipt(
                "journal-4", first.envelope.observation.observation_digest, 2
            ),
        )
        await runtime.close(100)
        store.close()

        reopened_store = mail.MailStateStore(path)
        try:
            reopened = mail.MailImapRuntime(
                _config(),
                _Credentials(),
                ledger,
                lambda *_a, **_k: _Imap(uidvalidity=7, uids=(4, 5)),
                state_store=reopened_store,
            )
            await reopened.start(_instance(), SimpleNamespace())
            resumed = await reopened.next_ingress()
            assert mail.MailCheckpoint.parse(resumed.checkpoint.token or "").uid == 5
        finally:
            reopened_store.close()

    asyncio.run(scenario())


def test_authenticated_mailbox_and_bcc_are_derived_from_bound_fetch_evidence() -> None:
    codec = mail.MailMessageCodec(
        _instance(),
        _config(),
        self_addresses=("alice@example.com", "alias@example.com"),
    )
    raw = _message(visible_self=False)
    evidence = mail.MailFetchEvidence(
        mailbox_scope="mailbox-1",
        authenticated_mailbox_id="alice@example.com",
        folder="INBOX",
        uidvalidity=7,
        uid=4,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
    )
    _normalized, platform = codec.decode_message(
        raw,
        fetch_evidence=evidence,
        raw_source_ref="sealed:mail:7:4",
        received_at_ms=10,
    )
    assert platform.envelope.authenticated_mailbox_id == "alice@example.com"
    assert platform.envelope.bcc_visibility == "self-only"
    assert tuple(row.address.address for row in platform.envelope.recipients) == (
        "bob@example.com",
    )
    assert "mallory@example.com" not in repr(platform.envelope)

    forged = mail.MailFetchEvidence(
        mailbox_scope="mailbox-1",
        authenticated_mailbox_id="mallory@example.com",
        folder="INBOX",
        uidvalidity=7,
        uid=4,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
    )
    with pytest.raises(ValueError, match="authenticated mailbox"):
        codec.decode_message(
            raw,
            fetch_evidence=forged,
            raw_source_ref="sealed:mail:7:4",
            received_at_ms=10,
        )


def test_visible_verified_self_alias_means_no_hidden_bcc() -> None:
    codec = mail.MailMessageCodec(
        _instance(),
        _config(),
        self_addresses=("alice@example.com", "alias@example.com"),
    )
    message = EmailMessage()
    message["From"] = "Ada <ada@example.com>"
    message["To"] = "Alice Alias <alias@example.com>"
    message["Bcc"] = "Mallory <mallory@example.com>"
    message["Message-ID"] = "<alias-visible@example.com>"
    message.set_content("hello")
    raw = message.as_bytes()
    evidence = mail.MailFetchEvidence(
        mailbox_scope="mailbox-1",
        authenticated_mailbox_id="alice@example.com",
        folder="INBOX",
        uidvalidity=7,
        uid=4,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
    )

    _normalized, platform = codec.decode_message(
        raw,
        fetch_evidence=evidence,
        raw_source_ref="sealed:mail:7:4",
        received_at_ms=10,
    )

    assert platform.envelope.bcc_visibility == "none"
    assert tuple(row.address.address for row in platform.envelope.recipients) == (
        "alias@example.com",
    )
    assert "mallory@example.com" not in repr(platform.envelope)


def test_authenticated_mailbox_domain_case_roundtrips_runtime_evidence_and_codec() -> None:
    async def scenario() -> None:
        config = _config(authenticated_mailbox_id="Alice@EXAMPLE.COM")
        runtime = mail.MailImapRuntime(
            config,
            _Credentials(),
            _Ledger(),
            lambda *_a, **_k: _Imap(uidvalidity=7, uids=(4,)),
        )
        await runtime.start(_instance(), SimpleNamespace())
        unit = await runtime.next_ingress()
        evidence = runtime.fetch_evidence(unit.envelope.observation.raw_safe_ref)
        assert evidence.authenticated_mailbox_id == "Alice@example.com"

        codec = mail.MailMessageCodec(
            _instance(),
            config,
            self_addresses=("Alice@example.com",),
        )
        _normalized, platform = codec.decode_message(
            runtime.raw_message(unit.envelope.observation.raw_safe_ref),
            fetch_evidence=evidence,
            raw_source_ref=unit.envelope.observation.raw_safe_ref,
            received_at_ms=10,
        )
        assert platform.envelope.authenticated_mailbox_id == "Alice@example.com"

    asyncio.run(scenario())


def _dsn(*, transaction_id: str = "tx-1", action: str = "failed") -> bytes:
    return (
        b"From: mailer-daemon@example.com\r\n"
        b"Content-Type: multipart/report; report-type=delivery-status; boundary=x\r\n\r\n"
        b"--x\r\nContent-Type: text/plain\r\n\r\nDelivery report\r\n"
        b"--x\r\nContent-Type: message/delivery-status\r\n\r\n"
        + f"Original-Envelope-Id: {transaction_id}\r\n\r\n".encode()
        + b"Final-Recipient: rfc822; bob@example.com\r\n"
        + f"Action: {action}\r\nStatus: 5.1.1\r\n\r\n".encode()
        + b"--x--\r\n"
    )


def _mdn() -> bytes:
    return (
        b"From: bob@example.com\r\n"
        b"Content-Type: multipart/report; report-type=disposition-notification; boundary=x\r\n\r\n"
        b"--x\r\nContent-Type: text/plain\r\n\r\nDisplayed\r\n"
        b"--x\r\nContent-Type: message/disposition-notification\r\n\r\n"
        b"Original-Message-ID: <out-1@example.com>\r\n"
        b"Final-Recipient: rfc822; bob@example.com\r\n"
        b"Disposition: manual-action/MDN-sent-manually; displayed\r\n\r\n"
        b"--x--\r\n"
    )


def test_dsn_mdn_stage_authenticate_reconcile_dedup_and_reopen(tmp_path: object) -> None:
    path = getattr(tmp_path, "joinpath")("mail-state.sqlite3")
    store = mail.MailStateStore(path)
    try:
        store.record_transaction(
            transaction_id="tx-1",
            outbound_message_id="out-1@example.com",
            recipients={"recipient-bob": "bob@example.com"},
        )
        processor = mail.MailReceiptProcessor(store, authenticator=lambda _message: True)
        receipt_id = processor.stage(_dsn(), source_message_id="dsn-1", observed_at_ms=10)
    finally:
        store.close()

    reopened_store = mail.MailStateStore(path)
    try:
        reopened = mail.MailReceiptProcessor(
            reopened_store, authenticator=lambda _message: True
        )
        assert reopened.reconcile_pending(limit=10) == (receipt_id,)
        assert reopened_store.delivery_status("tx-1", "recipient-bob") == "failed"
        duplicate = reopened.stage(_dsn(), source_message_id="dsn-2", observed_at_ms=11)
        assert reopened.reconcile_pending(limit=10) == (duplicate,)
        assert reopened_store.observation_count("tx-1", "recipient-bob") == 1

        mdn = reopened.stage(_mdn(), source_message_id="mdn-1", observed_at_ms=12)
        assert reopened.reconcile_pending(limit=10) == (mdn,)
        assert reopened_store.delivery_status("tx-1", "recipient-bob") == "displayed"
    finally:
        reopened_store.close()


def test_forged_or_oversized_receipts_do_not_transition_delivery(tmp_path: object) -> None:
    store = mail.MailStateStore(getattr(tmp_path, "joinpath")("mail-state.sqlite3"))
    try:
        store.record_transaction(
            transaction_id="tx-1",
            outbound_message_id="out-1@example.com",
            recipients={"recipient-bob": "bob@example.com"},
        )
        forged = mail.MailReceiptProcessor(store, authenticator=lambda _message: False)
        receipt_id = forged.stage(_dsn(), source_message_id="forged-1", observed_at_ms=10)
        assert forged.reconcile_pending(limit=10) == (receipt_id,)
        assert store.delivery_status("tx-1", "recipient-bob") == "accepted"
        assert store.observation_count("tx-1", "recipient-bob") == 0
        with pytest.raises(ValueError, match="size"):
            forged.stage(
                b"x" * (mail.MAX_RECEIPT_BYTES + 1),
                source_message_id="big",
                observed_at_ms=11,
            )
    finally:
        store.close()
