from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    CustodyReceipt,
    freeze_json_v1,
)
from hyprial.daemon.impl.adapters.imessage import (
    IMessageBlockedError,
    IMessageCodec,
    IMessageCursorLedger,
    IMessageRow,
    IMessageRuntime,
    IMessageSendObservation,
)


def _instance() -> AdapterInstanceRef:
    fingerprint = hashlib.sha256(b"imessage\0macos\0local-account").hexdigest()
    return AdapterInstanceRef(
        "imessage-1", "imessage", "macos-text-dm", "main", "org-1", fingerprint, 2
    )


class _Reader:
    def read_after(self, rowid: int, *, limit: int) -> tuple[IMessageRow, ...]:
        assert rowid in (0, 11)
        rows = (
            IMessageRow(10, "m-in", "chat-1", "+15550001", "hello", False, 100),
            IMessageRow(11, "m-self", "chat-1", "+15550001", "sent", True, 101),
        )
        return tuple(row for row in rows if row.rowid > rowid)[:limit]


class _Sender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def send_text(self, handle: str, text: str) -> IMessageSendObservation:
        self.calls.append((handle, text))
        return IMessageSendObservation("unknown", False, "IMESSAGE_RECEIPT_UNAVAILABLE")


def _operation() -> SimpleNamespace:
    return SimpleNamespace(
        sealed_request=freeze_json_v1(
            {
                "provider": "imessage",
                "organization_id": "org-1",
                "instance_id": "imessage-1",
                "generation": 2,
                "handle": "+15550001",
                "text": "reply",
                "part_index": 0,
            }
        ),
        kind="text-send",
        idempotency_key="im-1",
    )


def test_runtime_cursor_restart_echo_and_unknown_send_are_explicit(tmp_path: object) -> None:
    async def scenario() -> None:
        ledger = IMessageCursorLedger(getattr(tmp_path, "joinpath")("imessage.sqlite3"))
        try:
            sender = _Sender()
            runtime = IMessageRuntime(
                _Reader(),
                ledger,
                sender,
                account_ref="local-account",
                platform="darwin",
                full_disk_access_probe=lambda: (True, None),
                automation_probe=lambda: (True, None),
            )
            await runtime.start(_instance(), SimpleNamespace())
            incoming = await runtime.next_ingress()
            assert incoming.checkpoint.token == "10"
            await runtime.commit_checkpoint(
                incoming.checkpoint,
                CustodyReceipt("j-1", incoming.envelope.observation.observation_digest, 1),
            )
            self_echo = await runtime.next_ingress()
            assert dict(self_echo.envelope.payload)["is_from_me"] is True
            await runtime.commit_checkpoint(
                self_echo.checkpoint,
                CustodyReceipt("j-2", self_echo.envelope.observation.observation_digest, 2),
            )
            assert ledger.load_sync("imessage-1", 2) == 11

            restarted = IMessageRuntime(
                _Reader(),
                ledger,
                sender,
                account_ref="local-account",
                platform="darwin",
                full_disk_access_probe=lambda: (True, None),
                automation_probe=lambda: (True, None),
            )
            await restarted.start(_instance(), SimpleNamespace())
            with pytest.raises(StopAsyncIteration):
                await restarted.next_ingress()
            submission = await restarted.submit(_operation())
            assert submission.submissions[0].state == "unknown"
            assert submission.submissions[0].definitely_not_sent is False
            assert sender.calls == [("+15550001", "reply")]
        finally:
            ledger.close()

    asyncio.run(scenario())


def test_non_macos_and_permission_failures_report_typed_blocked_health(tmp_path: object) -> None:
    async def scenario() -> None:
        ledger = IMessageCursorLedger(getattr(tmp_path, "joinpath")("imessage.sqlite3"))
        try:
            runtime = IMessageRuntime(
                _Reader(), ledger, _Sender(), account_ref="local-account", platform="linux"
            )
            with pytest.raises(IMessageBlockedError, match="IMESSAGE_MACOS_REQUIRED"):
                await runtime.start(_instance(), SimpleNamespace())
            health = await runtime.health()
            assert health.state == "blocked"
            assert health.reason_codes == ("IMESSAGE_MACOS_REQUIRED",)

            denied = IMessageRuntime(
                _Reader(),
                ledger,
                _Sender(),
                account_ref="local-account",
                platform="darwin",
                full_disk_access_probe=lambda: (False, "IMESSAGE_FULL_DISK_ACCESS_REQUIRED"),
                automation_probe=lambda: (True, None),
            )
            with pytest.raises(IMessageBlockedError, match="IMESSAGE_FULL_DISK_ACCESS_REQUIRED"):
                await denied.start(_instance(), SimpleNamespace())
            assert (await denied.health()).reason_codes == (
                "IMESSAGE_FULL_DISK_ACCESS_REQUIRED",
            )
        finally:
            ledger.close()

    asyncio.run(scenario())


def test_absent_fda_or_automation_proof_blocks_and_health_remains_blocked(
    tmp_path: object,
) -> None:
    async def scenario() -> None:
        ledger = IMessageCursorLedger(getattr(tmp_path, "joinpath")("imessage.sqlite3"))
        try:
            absent = IMessageRuntime(
                _Reader(), ledger, _Sender(), account_ref="local-account", platform="darwin"
            )
            with pytest.raises(IMessageBlockedError, match="PROBE_REQUIRED"):
                await absent.start(_instance(), SimpleNamespace())
            assert (await absent.health()).state == "blocked"

            automation_denied = IMessageRuntime(
                _Reader(),
                ledger,
                _Sender(),
                account_ref="local-account",
                platform="darwin",
                full_disk_access_probe=lambda: (True, None),
                automation_probe=lambda: (False, "IMESSAGE_AUTOMATION_REQUIRED"),
            )
            with pytest.raises(IMessageBlockedError, match="IMESSAGE_AUTOMATION_REQUIRED"):
                await automation_denied.start(_instance(), SimpleNamespace())
            assert (await automation_denied.health()).reason_codes == (
                "IMESSAGE_AUTOMATION_REQUIRED",
            )
        finally:
            ledger.close()

    asyncio.run(scenario())


def test_imessage_checkpoint_rejects_forged_or_wrong_observation_custody(
    tmp_path: object,
) -> None:
    async def scenario() -> None:
        ledger = IMessageCursorLedger(getattr(tmp_path, "joinpath")("imessage.sqlite3"))
        try:
            runtime = IMessageRuntime(
                _Reader(),
                ledger,
                _Sender(),
                account_ref="local-account",
                platform="darwin",
                full_disk_access_probe=lambda: (True, None),
                automation_probe=lambda: (True, None),
            )
            await runtime.start(_instance(), SimpleNamespace())
            first = await runtime.next_ingress()
            second = await runtime.next_ingress()
            with pytest.raises(ValueError, match="custody"):
                await runtime.commit_checkpoint(
                    first.checkpoint,
                    CustodyReceipt(
                        "journal-2", second.envelope.observation.observation_digest, 1
                    ),
                )
            with pytest.raises(ValueError, match="custody"):
                await runtime.commit_checkpoint(
                    first.checkpoint, CustodyReceipt("forged", "forged", 1)
                )
            assert ledger.load_sync("imessage-1", 2) == 0
        finally:
            ledger.close()

    asyncio.run(scenario())


def test_inbound_chat_context_carries_the_adapter_uri() -> None:
    row = IMessageRow(10, "m-in", "iMessage;-;+15550001", "+15550001", "hi", False, 100)
    normalized, _provenance = IMessageCodec(_instance(), "local-account").decode_row(
        row, raw_source_ref="row:10", received_at_ms=100
    )

    assert normalized.context.channel_id == (
        "adapter:imessage:imessage-1:iMessage;-;+15550001"
    )
