"""macOS-only text-DM iMessage normalization and runtime."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable, Mapping
from typing import Protocol

from ..contracts import (
    AdapterCloseOutcome,
    AdapterHealth,
    AdapterInstanceRef,
    AudienceSnapshot,
    ChatContext,
    ChatRef,
    CustodyReceipt,
    HydratedEvent,
    IMessageProvenanceV1,
    IngressObservation,
    IngressSink,
    IngressUnit,
    IntentCapability,
    NativeAlias,
    NativeCheckpoint,
    NativeSubmissionOutcome,
    NormalizedIngress,
    PartSubmission,
    PlatformEventEnvelope,
    PlatformRefsV1,
    PlatformScopeV1,
    PlatformUnknownV1,
    PrincipalRef,
    RuntimeSession,
    SourceProvenance,
    TextBlock,
    frozen_json_digest_v1,
)
from .io import IMessageRow, IMessageSendObservation


class IMessageReader(Protocol):
    def read_after(self, rowid: int, *, limit: int) -> tuple[IMessageRow, ...]: ...


class IMessageSender(Protocol):
    def send_text(self, handle: str, text: str) -> IMessageSendObservation: ...


class IMessageLedger(Protocol):
    async def load(self, instance_id: str, generation: int) -> int: ...

    async def commit(self, instance_id: str, generation: int, rowid: int) -> None: ...


class IMessageBlockedError(RuntimeError):
    pass


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        if value and all(
            isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)
            for item in value
        ):
            return {item[0]: _thaw(item[1]) for item in value}
        return [_thaw(item) for item in value]
    return value


def _principal(handle: str, *, is_self: bool) -> PrincipalRef:
    return PrincipalRef(
        handle,
        None,
        None,
        None,
        None,
        "unresolved",
        None,
        is_self,
        None,
        "imessage-chat-db",
    )


class IMessageCodec:
    def __init__(self, instance: AdapterInstanceRef, account_ref: str) -> None:
        if instance.kind != "imessage" or instance.variant != "macos-text-dm":
            raise ValueError("iMessage codec requires imessage/macos-text-dm instance")
        self._instance = instance
        self._account_ref = account_ref

    def decode_row(
        self, row: IMessageRow, *, raw_source_ref: str, received_at_ms: int
    ) -> tuple[NormalizedIngress, IMessageProvenanceV1]:
        if not row.chat_guid.startswith("iMessage;-;"):
            raise ValueError("iMessage v1 supports only direct chats")
        sender = _principal(row.handle, is_self=row.is_from_me)
        chat_ref = ChatRef(
            f"chat-ref:v1:imessage:{self._instance.instance_id}:{row.chat_guid}",
            self._instance,
            self._account_ref,
            row.chat_guid,
            "dm",
            None,
            None,
        )
        context = ChatContext(
            f"adapter:imessage:{self._instance.instance_id}:{row.chat_guid}",
            chat_ref,
            None,
            sender,
            AudienceSnapshot("private", (sender,), "complete", None, 1),
            (),
            (
                IntentCapability(
                    "chat.reply.source", ("inherit", "corresponding", "exact"), True
                ),
                IntentCapability("chat.send.explicit", ("exact",), False),
            ),
            "imessage-macos-text-dm-v1",
        )
        alias = NativeAlias(
            self._instance.instance_id,
            self._instance.generation,
            self._account_ref,
            row.chat_guid,
            "create",
            "message",
            row.message_guid,
            "imessage-chat-db-v1",
        )
        observation_id = f"imessage:{self._instance.instance_id}:{row.rowid}"
        provenance = SourceProvenance(
            f"ingress:{observation_id}",
            (observation_id,),
            (alias,),
            received_at_ms,
            row.date,
            "bridge-mirror" if row.is_from_me else "native-human",
            row.handle,
        )
        platform = IMessageProvenanceV1(
            "hyprial.chat.platform.imessage/v1",
            PlatformScopeV1(
                self._instance.organization_id,
                self._instance.instance_id,
                self._instance.generation,
                self._account_ref,
                "macos-local",
            ),
            PlatformRefsV1(message_id=row.message_guid),
            PlatformUnknownV1({}, raw_source_ref),
            self._account_ref,
            row.chat_guid,
            row.message_guid,
            row.rowid,
            row.handle,
            row.is_from_me,
        )
        return (
            NormalizedIngress(context, provenance, (TextBlock("text", row.text),), (), None),
            platform,
        )


class IMessageRuntime:
    def __init__(
        self,
        reader: IMessageReader,
        ledger: IMessageLedger,
        sender: IMessageSender,
        *,
        account_ref: str,
        platform: str | None = None,
        full_disk_access_probe: Callable[[], tuple[bool, str | None]] | None = None,
        automation_probe: Callable[[], tuple[bool, str | None]] | None = None,
        page_size: int = 200,
    ) -> None:
        if not account_ref:
            raise ValueError("account_ref must not be empty")
        if not 1 <= page_size <= 500:
            raise ValueError("iMessage page size must be in 1..500")
        self._reader = reader
        self._ledger = ledger
        self._sender = sender
        self._account_ref = account_ref
        self._platform = platform or sys.platform
        self._full_disk_access_probe = full_disk_access_probe
        self._automation_probe = automation_probe
        self._page_size = page_size
        self._instance: AdapterInstanceRef | None = None
        self._cursor = 0
        self._pending: list[IMessageRow] = []
        self._rows: dict[str, IMessageRow] = {}
        self._pending_checkpoints: dict[str, tuple[str, str, int]] = {}
        self._checkpoint_order: list[str] = []
        self._state = "configured"
        self._reason_codes: tuple[str, ...] = ()

    async def start(self, instance: AdapterInstanceRef, sink: IngressSink) -> RuntimeSession:
        del sink
        if instance.kind != "imessage" or instance.variant != "macos-text-dm":
            raise ValueError("iMessage runtime requires imessage/macos-text-dm instance")
        reason = None
        if self._platform != "darwin":
            reason = "IMESSAGE_MACOS_REQUIRED"
        elif self._full_disk_access_probe is None:
            reason = "IMESSAGE_FULL_DISK_ACCESS_PROBE_REQUIRED"
        elif self._automation_probe is None:
            reason = "IMESSAGE_AUTOMATION_PROBE_REQUIRED"
        else:
            granted, permission_reason = await asyncio.to_thread(
                self._full_disk_access_probe
            )
            if not granted:
                reason = permission_reason or "IMESSAGE_FULL_DISK_ACCESS_REQUIRED"
            else:
                granted, permission_reason = await asyncio.to_thread(
                    self._automation_probe
                )
                if not granted:
                    reason = permission_reason or "IMESSAGE_AUTOMATION_REQUIRED"
        if reason:
            self._state = "blocked"
            self._reason_codes = (reason,)
            raise IMessageBlockedError(reason)
        self._instance = instance
        self._cursor = await self._ledger.load(instance.instance_id, instance.generation)
        self._state = "online"
        return RuntimeSession(
            f"imessage:{instance.instance_id}:{instance.generation}",
            instance.instance_id,
            instance.generation,
            "pull",
        )

    async def next_ingress(self) -> IngressUnit:
        instance = self._require_online()
        if not self._pending:
            self._pending = list(
                await asyncio.to_thread(
                    self._reader.read_after, self._cursor, limit=self._page_size
                )
            )
        if not self._pending:
            raise StopAsyncIteration
        row = self._pending.pop(0)
        raw_ref = f"sealed:imessage:{instance.instance_id}:{row.rowid}"
        self._rows[raw_ref] = row
        payload = {
            "rowid": row.rowid,
            "message_guid": row.message_guid,
            "chat_guid": row.chat_guid,
            "handle": row.handle,
            "text": row.text,
            "is_from_me": row.is_from_me,
            "date": row.date,
        }
        digest = frozen_json_digest_v1(payload)
        observation = IngressObservation(
            f"imessage:{instance.instance_id}:{row.rowid}",
            (
                NativeAlias(
                    instance.instance_id,
                    instance.generation,
                    self._account_ref,
                    row.chat_guid,
                    "create",
                    "message",
                    row.message_guid,
                    "imessage-chat-db-v1",
                ),
            ),
            "poll",
            raw_ref,
            digest,
            int(time.time() * 1000),
        )
        checkpoint = NativeCheckpoint("poll-cursor", str(row.rowid), None)
        if checkpoint.token is None:  # pragma: no cover - NativeCheckpoint invariant
            raise RuntimeError("iMessage checkpoint token unexpectedly absent")
        self._pending_checkpoints[checkpoint.token] = (
            digest,
            instance.instance_id,
            instance.generation,
        )
        self._checkpoint_order.append(checkpoint.token)
        return IngressUnit(
            PlatformEventEnvelope(observation, payload),
            checkpoint,
            False,
        )

    async def commit_checkpoint(
        self, checkpoint: NativeCheckpoint, custody: CustodyReceipt
    ) -> None:
        instance = self._require_online()
        if checkpoint.kind != "poll-cursor" or checkpoint.token is None:
            raise ValueError("iMessage requires a poll cursor checkpoint")
        rowid = int(checkpoint.token)
        pending = self._pending_checkpoints.get(checkpoint.token)
        if (
            pending is None
            or pending != (custody.envelope_digest, instance.instance_id, instance.generation)
            or not self._checkpoint_order
            or self._checkpoint_order[0] != checkpoint.token
        ):
            raise ValueError("iMessage checkpoint custody does not match the pending observation")
        if rowid < self._cursor:
            raise ValueError("iMessage cursor cannot move backwards")
        await self._ledger.commit(instance.instance_id, instance.generation, rowid)
        self._pending_checkpoints.pop(checkpoint.token, None)
        self._checkpoint_order.pop(0)
        self._cursor = rowid

    async def hydrate(self, envelope: PlatformEventEnvelope) -> HydratedEvent:
        self._require_online()
        return HydratedEvent(envelope, envelope.payload)

    async def submit(self, operation: object) -> NativeSubmissionOutcome:
        instance = self._require_online()
        request = _thaw(getattr(operation, "sealed_request", None))
        if not isinstance(request, Mapping):
            raise ValueError("iMessage operation requires a sealed request")
        expected = {
            "provider": "imessage",
            "organization_id": instance.organization_id,
            "instance_id": instance.instance_id,
            "generation": instance.generation,
        }
        if any(request.get(key) != value for key, value in expected.items()):
            raise ValueError("iMessage operation scope mismatch")
        if getattr(operation, "kind", None) != "text-send":
            return NativeSubmissionOutcome(
                (PartSubmission(int(request.get("part_index", 0)), "unsupported", None, True,
                                "IMESSAGE_TEXT_DM_ONLY"),),
                None,
            )
        handle = request.get("handle")
        text = request.get("text")
        if not isinstance(handle, str) or not handle or not isinstance(text, str) or not text:
            return NativeSubmissionOutcome(
                (PartSubmission(int(request.get("part_index", 0)), "failed-final", None, True,
                                "IMESSAGE_TARGET_OR_TEXT_INVALID"),),
                None,
            )
        observation = await asyncio.to_thread(self._sender.send_text, handle, text)
        return NativeSubmissionOutcome(
            (
                PartSubmission(
                    int(request.get("part_index", 0)),
                    "unknown" if observation.state == "unknown" else "failed-final",
                    None,
                    observation.definitely_not_sent,
                    observation.reason_code,
                ),
            ),
            None,
        )

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            self._state,
            "imessage-macos-text-dm-v1",
            len(self._pending),
            0,
            self._reason_codes,
        )

    async def close(self, deadline_ms: int) -> AdapterCloseOutcome:
        del deadline_ms
        retained = len(self._pending)
        self._state = "stopped"
        return AdapterCloseOutcome(
            "closed" if retained == 0 else "retained-pending",
            retained,
            0,
            None if retained == 0 else "IMESSAGE_CURSOR_PENDING",
        )

    def codec(self) -> IMessageCodec:
        return IMessageCodec(self._require_online(), self._account_ref)

    def _require_online(self) -> AdapterInstanceRef:
        if self._instance is None or self._state != "online":
            raise IMessageBlockedError(self._reason_codes[0] if self._reason_codes else "IMESSAGE_NOT_ONLINE")
        return self._instance


__all__ = [
    "IMessageBlockedError",
    "IMessageCodec",
    "IMessageLedger",
    "IMessageReader",
    "IMessageRuntime",
    "IMessageSender",
]
