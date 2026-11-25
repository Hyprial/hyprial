from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from hyprial.daemon.impl.adapters.contracts import (
    AdapterCloseOutcome,
    AdapterHealth,
    AdapterInstanceRef,
    AuthorizedChatTarget,
    CustodyReceipt,
    IngressObservation,
    IngressUnit,
    HydratedEvent,
    IngressSink,
    NativeCheckpoint,
    NativeSubmissionOutcome,
    NativeTargetResolution,
    OperationNode,
    PartSubmission,
    PlatformEventEnvelope,
    RuntimeSession,
    VerifiedCallbackRequest,
    canonical_json_v1,
    freeze_json_v1,
)

from .codec import decode_wecom_bot_frame, frame_digest, sanitize_wecom_bot_frame
from .errors import WeComProtocolError


@dataclass(frozen=True, slots=True)
class WeComBotCredentials:
    instance_id: str
    organization_id: str
    generation: int
    bot_id: str
    bot_secret: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.instance_id,
                self.organization_id,
                self.bot_id,
                self.bot_secret,
            )
        ):
            raise WeComProtocolError("missing-bot-credentials")
        if self.generation < 1:
            raise WeComProtocolError("invalid-generation")

    def __repr__(self) -> str:
        return (
            f"WeComBotCredentials(instance_id={self.instance_id!r}, "
            f"organization_id={self.organization_id!r}, generation={self.generation!r}, "
            f"bot_id={self.bot_id!r}, bot_secret=<redacted>)"
        )


class WeComBotTransport(Protocol):
    async def connect(self, credentials: WeComBotCredentials, receiver: object) -> None: ...

    async def disconnect(self) -> None: ...

    async def send(self, request: Mapping[str, object]) -> Mapping[str, object]: ...


@dataclass(slots=True)
class _Pending:
    req_id: str
    sequence: int
    digest: str
    unit: IngressUnit
    committed: bool = False


class WeComBotRuntime:
    def __init__(self, credentials: WeComBotCredentials, transport: WeComBotTransport) -> None:
        if not isinstance(credentials, WeComBotCredentials):
            raise TypeError("WeCom bot credentials are required for the bot variant")
        self._credentials = credentials
        self._transport = transport
        self._instance: AdapterInstanceRef | None = None
        self._sink: object | None = None
        self._pending: dict[str, _Pending] = {}
        self._history: list[IngressUnit] = []
        self._arrival_sequence = 0
        self._last_committed_sequence: int | None = None
        self._state = "configured"

    def _fence(self, instance: AdapterInstanceRef) -> None:
        if instance.kind != "wecom" or instance.variant != "bot":
            raise WeComProtocolError("variant-mismatch")
        if instance.instance_id != self._credentials.instance_id:
            raise WeComProtocolError("instance-mismatch")
        if instance.organization_id != self._credentials.organization_id:
            raise WeComProtocolError("organization-mismatch")
        if instance.generation != self._credentials.generation:
            raise WeComProtocolError("generation-mismatch")

    async def start(self, instance: AdapterInstanceRef, sink: IngressSink) -> RuntimeSession:
        self._fence(instance)
        self._state = "starting"
        await self._transport.connect(self._credentials, self)
        self._instance = instance
        self._sink = sink
        self._state = "online"
        return RuntimeSession(
            f"wecom-bot:{instance.instance_id}:{instance.generation}",
            instance.instance_id,
            instance.generation,
            "pull",
        )

    def receive_frame(self, frame: Mapping[str, object]) -> IngressUnit:
        if self._instance is None:
            raise WeComProtocolError("runtime-not-started")
        copied = dict(frame)
        digest = frame_digest(copied)
        decoded = decode_wecom_bot_frame(
            self._instance, copied, raw_source_ref=f"sealed-wecom-bot:{digest}"
        )
        token = f"wecom-bot-checkpoint:{uuid.uuid4().hex}"
        self._arrival_sequence += 1
        unit = IngressUnit(
            PlatformEventEnvelope(
                IngressObservation(
                    f"wecom-bot-observation:{uuid.uuid4().hex}",
                    decoded.aliases,
                    "live",
                    decoded.provenance.unknown.raw_source_ref,
                    digest,
                    int(time.time() * 1000),
                ),
                freeze_json_v1(sanitize_wecom_bot_frame(copied)),
            ),
            NativeCheckpoint("stream-sequence", token, None),
            False,
        )
        self._pending[token] = _Pending(
            decoded.provenance.req_id, self._arrival_sequence, digest, unit
        )
        self._history.append(unit)
        return unit

    async def ingest_frame(self, frame: Mapping[str, object]) -> IngressUnit:
        unit = self.receive_frame(frame)
        stage = getattr(self._sink, "stage", None)
        if not callable(stage):
            raise WeComProtocolError("ingress-sink-unavailable")
        custody = await stage(unit)
        if not isinstance(custody, CustodyReceipt):
            raise WeComProtocolError("ingress-custody-receipt-invalid")
        await self.commit_checkpoint(unit.checkpoint, custody)
        return unit

    async def next_ingress(self) -> IngressUnit:
        raise WeComProtocolError("pull-unavailable-for-sdk-push-runtime")

    async def accept_callback(self, request: VerifiedCallbackRequest) -> IngressUnit:
        raise WeComProtocolError("callback-unavailable-for-websocket-runtime")

    async def hydrate(self, envelope: PlatformEventEnvelope) -> HydratedEvent:
        if not isinstance(envelope, PlatformEventEnvelope):
            raise WeComProtocolError("event-envelope-required")
        return HydratedEvent(envelope, envelope.payload)

    async def history(
        self, after: NativeCheckpoint | None, limit: int
    ) -> tuple[IngressUnit, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise WeComProtocolError("invalid-history-limit")
        start = 0
        if after is not None:
            for index, unit in enumerate(self._history):
                if unit.checkpoint == after:
                    start = index + 1
                    break
            else:
                raise WeComProtocolError("history-checkpoint-unknown")
        return tuple(self._history[start : start + limit])

    async def resolve(self, target: AuthorizedChatTarget) -> NativeTargetResolution:
        if self._instance is None:
            raise WeComProtocolError("runtime-not-started")
        if target.chat_ref.instance != self._instance:
            return NativeTargetResolution(
                target, "unauthorized", None, None, "wecom-bot-target-instance-mismatch"
            )
        if target.chat_ref.kind == "thread":
            return NativeTargetResolution(
                target, "unsupported", None, None, "wecom-bot-thread-unsupported"
            )
        return NativeTargetResolution(
            target,
            "resolved",
            target.chat_ref.native_chat_id,
            target.chat_ref.native_thread_id,
            None,
        )

    def pending_units(self) -> tuple[IngressUnit, ...]:
        return tuple(
            item.unit
            for item in sorted(self._pending.values(), key=lambda item: item.sequence)
            if not item.committed
        )

    async def commit_checkpoint(
        self, checkpoint: NativeCheckpoint, custody: CustodyReceipt
    ) -> None:
        pending = self._pending.get(checkpoint.token or "")
        if pending is None:
            raise WeComProtocolError("unknown-checkpoint")
        if custody.envelope_digest != pending.digest:
            raise WeComProtocolError("custody-digest-mismatch")
        if pending.committed:
            return
        lower = [
            item
            for item in self._pending.values()
            if not item.committed and item.sequence < pending.sequence
        ]
        if lower:
            raise WeComProtocolError("stream-order-gap")
        pending.committed = True
        self._last_committed_sequence = pending.sequence

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            self._state,
            "wecom-aibot/1.0.2",
            sum(not item.committed for item in self._pending.values()),
            0,
            (() if self._state == "online" else (f"wecom-bot-{self._state}",)),
        )

    async def submit(self, operation: OperationNode) -> NativeSubmissionOutcome:
        if self._instance is None:
            raise WeComProtocolError("runtime-not-started")
        if operation.sealed_request is None:
            raise WeComProtocolError("sealed-request-required")
        request = json.loads(canonical_json_v1(operation.sealed_request))
        if not isinstance(request, dict):
            raise WeComProtocolError("sealed-request-invalid")
        for field, expected in (
            ("instanceId", self._instance.instance_id),
            ("generation", self._instance.generation),
            ("organizationId", self._instance.organization_id),
        ):
            if request.get(field) != expected:
                raise WeComProtocolError(f"{field}-mismatch")
        try:
            result = await self._transport.send(request)
        except Exception:
            submission = PartSubmission(
                0, "unknown", None, False, "wecom-bot-submit-unknown"
            )
        else:
            headers = result.get("headers")
            provider_id = (
                headers.get("req_id") if isinstance(headers, Mapping) else None
            ) or result.get("messageId")
            if isinstance(provider_id, str) and provider_id:
                submission = PartSubmission(0, "accepted", provider_id, False, None)
            else:
                submission = PartSubmission(
                    0, "unknown", None, False, "wecom-bot-submit-missing-id"
                )
        return NativeSubmissionOutcome((submission,), None)

    async def reconcile(
        self, operation: OperationNode, prior: NativeSubmissionOutcome
    ) -> NativeSubmissionOutcome:
        # The SDK has no authoritative lookup for a timed-out req_id. Keep
        # unknown custody and never issue a second send automatically.
        return prior

    async def reconnect(self, reason: str) -> None:
        if self._instance is None:
            raise WeComProtocolError("runtime-not-started")
        if not isinstance(reason, str) or not reason:
            raise WeComProtocolError("reconnect-reason-required")
        self._state = "reconnecting"
        await self._transport.disconnect()
        await self._transport.connect(self._credentials, self)
        self._state = "online"

    async def close(self, deadline_ms: int) -> AdapterCloseOutcome:
        await self._transport.disconnect()
        self._state = "stopped"
        retained = sum(not item.committed for item in self._pending.values())
        return AdapterCloseOutcome(
            "closed" if retained == 0 else "retained-pending",
            retained,
            0,
            None if retained == 0 else "wecom-bot-unacked-ingress",
        )


__all__ = ["WeComBotCredentials", "WeComBotRuntime", "WeComBotTransport"]
