from __future__ import annotations

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
    PlatformEventEnvelope,
    RuntimeSession,
    VerifiedCallbackRequest,
    canonical_json_v1,
    freeze_json_v1,
)

from .codec import DingTalkReplyRouteStore, decode_dingtalk_message, payload_digest
from .errors import DingTalkProtocolError


@dataclass(frozen=True, slots=True)
class DingTalkCredentials:
    instance_id: str
    organization_id: str
    generation: int
    client_id: str
    client_secret: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.instance_id,
                self.organization_id,
                self.client_id,
                self.client_secret,
            )
        ):
            raise DingTalkProtocolError("missing-credentials")
        if self.generation < 1:
            raise DingTalkProtocolError("invalid-generation")

    def __repr__(self) -> str:
        return (
            f"DingTalkCredentials(instance_id={self.instance_id!r}, "
            f"organization_id={self.organization_id!r}, generation={self.generation!r}, "
            f"client_id={self.client_id!r}, client_secret=<redacted>)"
        )


class DingTalkStreamTransport(Protocol):
    async def start(self, credentials: DingTalkCredentials, receiver: object) -> None: ...

    async def stop(self) -> None: ...

    async def reply(self, webhook: str, payload: dict[str, object]) -> Mapping[str, object]: ...


@dataclass(slots=True)
class _Pending:
    delivery_id: str
    digest: str
    committed: bool = False


class DingTalkStreamRuntime:
    def __init__(
        self,
        credentials: DingTalkCredentials,
        transport: DingTalkStreamTransport,
        *,
        routes: DingTalkReplyRouteStore | None = None,
        now_ms: object | None = None,
    ) -> None:
        if not isinstance(credentials, DingTalkCredentials):
            raise TypeError("DingTalkStreamRuntime requires receiver-owned credentials")
        self._credentials = credentials
        self._transport = transport
        self.routes = routes or DingTalkReplyRouteStore()
        self._now_ms = now_ms if callable(now_ms) else lambda: int(time.time() * 1000)
        self._instance: AdapterInstanceRef | None = None
        self._sink: object | None = None
        self._pending: dict[str, _Pending] = {}
        self._history: list[IngressUnit] = []
        self._state = "configured"

    def _fence(self, instance: AdapterInstanceRef) -> None:
        if instance.kind != "dingtalk" or instance.variant != "stream":
            raise DingTalkProtocolError("variant-mismatch")
        if instance.instance_id != self._credentials.instance_id:
            raise DingTalkProtocolError("instance-mismatch")
        if instance.organization_id != self._credentials.organization_id:
            raise DingTalkProtocolError("organization-mismatch")
        if instance.generation != self._credentials.generation:
            raise DingTalkProtocolError("generation-mismatch")

    async def start(self, instance: AdapterInstanceRef, sink: IngressSink) -> RuntimeSession:
        self._fence(instance)
        self._state = "starting"
        await self._transport.start(self._credentials, self)
        self._instance = instance
        self._sink = sink
        self._state = "online"
        return RuntimeSession(
            f"dingtalk:{instance.instance_id}:{instance.generation}",
            instance.instance_id,
            instance.generation,
            "pull",
        )

    def receive_stream(self, delivery_id: str, payload: Mapping[str, object]) -> IngressUnit:
        if self._instance is None:
            raise DingTalkProtocolError("runtime-not-started")
        if not delivery_id:
            raise DingTalkProtocolError("missing-delivery-id")
        copied = dict(payload)
        digest = payload_digest(copied)
        decoded = decode_dingtalk_message(
            self._instance,
            copied,
            routes=self.routes,
            raw_source_ref=f"sealed-dingtalk:{digest}",
        )
        token = f"dingtalk-checkpoint:{uuid.uuid4().hex}"
        self._pending[token] = _Pending(delivery_id, digest)
        unit = IngressUnit(
            PlatformEventEnvelope(
                IngressObservation(
                    f"dingtalk-observation:{uuid.uuid4().hex}",
                    decoded.aliases,
                    "live",
                    decoded.provenance.unknown.raw_source_ref,
                    digest,
                    self._now_ms(),
                ),
                freeze_json_v1(
                    {
                        **{key: value for key, value in copied.items() if key != "sessionWebhook"},
                        **(
                            {"sessionWebhookRef": decoded.provenance.session_webhook_ref}
                            if decoded.provenance.session_webhook_ref
                            else {}
                        ),
                    }
                ),
            ),
            NativeCheckpoint("stream-sequence", token, None),
            False,
        )
        self._history.append(unit)
        return unit

    async def ingest_stream(
        self, delivery_id: str, payload: Mapping[str, object]
    ) -> IngressUnit:
        unit = self.receive_stream(delivery_id, payload)
        stage = getattr(self._sink, "stage", None)
        if not callable(stage):
            raise DingTalkProtocolError("ingress-sink-unavailable")
        custody = await stage(unit)
        if not isinstance(custody, CustodyReceipt):
            raise DingTalkProtocolError("ingress-custody-receipt-invalid")
        await self.commit_checkpoint(unit.checkpoint, custody)
        return unit

    async def next_ingress(self) -> IngressUnit:
        raise DingTalkProtocolError("pull-unavailable-for-sdk-push-runtime")

    async def accept_callback(self, request: VerifiedCallbackRequest) -> IngressUnit:
        raise DingTalkProtocolError("callback-unavailable-for-stream-runtime")

    async def hydrate(self, envelope: PlatformEventEnvelope) -> HydratedEvent:
        if not isinstance(envelope, PlatformEventEnvelope):
            raise DingTalkProtocolError("event-envelope-required")
        return HydratedEvent(envelope, envelope.payload)

    async def history(
        self, after: NativeCheckpoint | None, limit: int
    ) -> tuple[IngressUnit, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise DingTalkProtocolError("invalid-history-limit")
        start = 0
        if after is not None:
            for index, unit in enumerate(self._history):
                if unit.checkpoint == after:
                    start = index + 1
                    break
            else:
                raise DingTalkProtocolError("history-checkpoint-unknown")
        return tuple(self._history[start : start + limit])

    async def resolve(self, target: AuthorizedChatTarget) -> NativeTargetResolution:
        if self._instance is None:
            raise DingTalkProtocolError("runtime-not-started")
        if target.chat_ref.instance != self._instance:
            return NativeTargetResolution(
                target, "unauthorized", None, None, "dingtalk-target-instance-mismatch"
            )
        try:
            route_ref = self.routes.resolve_ref(
                target.chat_ref.native_chat_id,
                instance_id=self._instance.instance_id,
                generation=self._instance.generation,
            )
        except DingTalkProtocolError:
            return NativeTargetResolution(
                target, "unresolved", None, None, "dingtalk-reply-route-required"
            )
        return NativeTargetResolution(
            target,
            "resolved",
            target.chat_ref.native_chat_id,
            route_ref,
            None,
        )

    async def commit_checkpoint(
        self, checkpoint: NativeCheckpoint, custody: CustodyReceipt
    ) -> None:
        pending = self._pending.get(checkpoint.token or "")
        if pending is None:
            raise DingTalkProtocolError("unknown-checkpoint")
        if custody.envelope_digest != pending.digest:
            raise DingTalkProtocolError("custody-digest-mismatch")
        if pending.committed:
            return
        pending.committed = True

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            self._state,
            "dingtalk-stream/0.24.3",
            sum(not item.committed for item in self._pending.values()),
            0,
            (() if self._state == "online" else (f"dingtalk-{self._state}",)),
        )

    async def submit(self, operation: OperationNode) -> NativeSubmissionOutcome:
        import json

        from .codec import classify_dingtalk_card_step

        if self._instance is None:
            raise DingTalkProtocolError("runtime-not-started")
        if operation.sealed_request is None:
            raise DingTalkProtocolError("sealed-request-required")
        request = json.loads(canonical_json_v1(operation.sealed_request))
        if not isinstance(request, dict):
            raise DingTalkProtocolError("sealed-request-invalid")
        for field, expected in (
            ("instanceId", self._instance.instance_id),
            ("generation", self._instance.generation),
            ("organizationId", self._instance.organization_id),
        ):
            if request.get(field) != expected:
                raise DingTalkProtocolError(f"{field}-mismatch")
        route_ref = request.get("sessionWebhookRef")
        if not isinstance(route_ref, str):
            from hyprial.daemon.impl.adapters.contracts import PartSubmission

            return NativeSubmissionOutcome(
                (PartSubmission(0, "unsupported", None, True, "dingtalk-reply-route-required"),),
                None,
            )
        webhook = self.routes.resolve(
            route_ref,
            instance_id=self._instance.instance_id,
            generation=self._instance.generation,
        )
        text = request.get("text")
        if not isinstance(text, str) or not text:
            from hyprial.daemon.impl.adapters.contracts import PartSubmission

            return NativeSubmissionOutcome(
                (PartSubmission(0, "failed-retryable", None, True, "dingtalk-text-required"),),
                None,
            )
        webhook_body = {"msgtype": "text", "text": {"content": text}}
        try:
            result = await self._transport.reply(webhook, webhook_body)
        except Exception as exc:
            submission = classify_dingtalk_card_step(
                step="reply", result=exc, operation_started=True
            )
        else:
            submission = classify_dingtalk_card_step(
                step="reply", result=result, operation_started=True
            )
        return NativeSubmissionOutcome((submission,), None)

    async def reconcile(
        self, operation: OperationNode, prior: NativeSubmissionOutcome
    ) -> NativeSubmissionOutcome:
        # sessionWebhook sends have no durable provider query. Card operations
        # keep their outTrackId in the sealed request and are reconciled by the
        # card-specific transport owner; never create a replacement card here.
        return prior

    async def reconnect(self, reason: str) -> None:
        if self._instance is None:
            raise DingTalkProtocolError("runtime-not-started")
        if not isinstance(reason, str) or not reason:
            raise DingTalkProtocolError("reconnect-reason-required")
        self._state = "reconnecting"
        await self._transport.stop()
        await self._transport.start(self._credentials, self)
        self._state = "online"

    async def close(self, deadline_ms: int) -> AdapterCloseOutcome:
        await self._transport.stop()
        self._state = "stopped"
        retained = sum(not item.committed for item in self._pending.values())
        return AdapterCloseOutcome(
            "closed" if retained == 0 else "retained-pending",
            retained,
            0,
            None if retained == 0 else "dingtalk-unacked-ingress",
        )


__all__ = ["DingTalkCredentials", "DingTalkStreamRuntime", "DingTalkStreamTransport"]
