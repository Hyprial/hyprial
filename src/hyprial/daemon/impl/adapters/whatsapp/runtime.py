"""Daemon-side WhatsApp runtime over the authenticated generic worker."""
# hyprial-unbounded-wait-allow: get - awaited asyncio.Queue ingress is cancellable by its owning task

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from hyprial.daemon.impl.adapters.contracts import (
    AdapterCloseOutcome,
    AdapterHealth,
    AuthorizedChatTarget,
    CustodyReceipt,
    HydratedEvent,
    IngressObservation,
    IngressUnit,
    NativeAlias,
    NativeCheckpoint,
    NativeSubmissionOutcome,
    NativeTargetResolution,
    PartSubmission,
    PlatformEventEnvelope,
    RuntimeSession,
    VerifiedCallbackRequest,
    canonical_json_v1,
    freeze_json_v1,
    frozen_json_digest_v1,
)

from .boundary import (
    ArtifactStatus,
    NativeWorker,
    WhatsAppEffectFence,
    WhatsAppProviderError,
    invoke_native,
    require_artifact,
)


def _json(value: object) -> object:
    import json

    return json.loads(canonical_json_v1(freeze_json_v1(value)))


def _parts(response: Mapping[str, object]) -> NativeSubmissionOutcome:
    raw = response.get("parts")
    if isinstance(raw, (str, bytes, bytearray, memoryview)) or not isinstance(raw, Sequence):
        raise WhatsAppProviderError("WHATSAPP_NATIVE_RESPONSE_INVALID", "parts must be an array")
    parts: list[PartSubmission] = []
    try:
        for item in raw:
            if not isinstance(item, Mapping):
                raise TypeError("part must be an object")
            parts.append(
                PartSubmission(
                    part_index=item["partIndex"],
                    state=item["state"],
                    provider_message_id=item.get("providerMessageId"),
                    definitely_not_sent=item["definitelyNotSent"],
                    reason_code=item.get("reasonCode"),
                )
            )
    except (KeyError, TypeError, ValueError) as error:
        raise WhatsAppProviderError("WHATSAPP_NATIVE_RESPONSE_INVALID", str(error)) from error
    return NativeSubmissionOutcome(tuple(parts), response.get("retryAfterMs"))


class WhatsAppRuntime:
    def __init__(
        self,
        worker: NativeWorker,
        *,
        instance,
        account_jid: str,
        artifact: ArtifactStatus,
        effect_fence: WhatsAppEffectFence | None = None,
    ) -> None:
        self.worker = worker
        self.instance = instance
        self.account_jid = account_jid
        self.artifact = artifact
        self.effect_fence = effect_fence or WhatsAppEffectFence(instance, account_jid)
        self._ingress: asyncio.Queue[IngressUnit] = asyncio.Queue(maxsize=256)
        self._sink = None
        self._unknown: set[str] = set()
        self._blocked: set[str] = set()
        self._revoked = False

    @property
    def pending_ingress(self) -> int:
        return self._ingress.qsize()

    async def start(self, instance, sink) -> RuntimeSession:
        require_artifact(self.artifact)
        if instance != self.instance:
            raise WhatsAppProviderError("WHATSAPP_INSTANCE_MISMATCH", "runtime instance changed")
        response = await invoke_native(
            self.worker,
            "runtime.start",
            {
                "instanceId": instance.instance_id,
                "generation": instance.generation,
                "accountJid": self.account_jid,
            },
        )
        try:
            session = RuntimeSession(
                response["sessionId"],
                instance.instance_id,
                instance.generation,
                response["ingressMode"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise WhatsAppProviderError("WHATSAPP_NATIVE_RESPONSE_INVALID", str(error)) from error
        self._sink = sink
        return session

    def accept_native_callback(self, value: object) -> bool:
        if not isinstance(value, Mapping):
            return False
        generation = value.get("generation", self.instance.generation)
        if generation != self.instance.generation or self._revoked:
            return False
        callback_type = value.get("type")
        if callback_type == "crash":
            reason = value.get("reasonCode")
            code = reason if isinstance(reason, str) and reason else "WHATSAPP_NATIVE_CRASH"
            self._blocked.add(code)
            self.effect_fence.block(code)
            return True
        if callback_type != "event" or not isinstance(value.get("event"), Mapping):
            return False
        try:
            unit = self._unit(dict(value["event"]), value.get("cursor"))
            self._ingress.put_nowait(unit)
        except (ValueError, TypeError, asyncio.QueueFull):
            self._blocked.add("WHATSAPP_INGRESS_CAPACITY")
            return False
        return True

    def _unit(self, event: dict[str, object], cursor: object) -> IngressUnit:
        message_id = event.get("messageId")
        chat_jid = event.get("chatJid")
        if not isinstance(message_id, str) or not message_id or not isinstance(chat_jid, str) or not chat_jid:
            raise ValueError("event requires message/chat ids")
        event_kind = event.get("eventKind")
        semantic = "edit" if event_kind == "edit" else "reaction" if event_kind == "reaction" else "create"
        alias = NativeAlias(
            instance_id=self.instance.instance_id,
            instance_generation=self.instance.generation,
            account_scope=self.account_jid,
            provider_resource=chat_jid,
            semantic_kind=semantic,
            alias_kind="message",
            normalized_id=message_id,
            normalized_revision="whatsapp-linked-device/v1",
            platform="whatsapp",
            chat_scope=chat_jid,
        )
        frozen = freeze_json_v1(event)
        envelope = PlatformEventEnvelope(
            observation=IngressObservation(
                observation_id=f"obs:{self.instance.instance_id}:{message_id}:{semantic}",
                aliases=(alias,),
                source_kind="live",
                raw_safe_ref=f"whatsapp-event:{self.instance.instance_id}:{message_id}",
                observation_digest=frozen_json_digest_v1(frozen),
                received_at_ms=int(event.get("receivedAtMs", 0)),
            ),
            payload=frozen,
        )
        checkpoint = NativeCheckpoint(
            "poll-cursor" if isinstance(cursor, str) and cursor else "none",
            cursor if isinstance(cursor, str) and cursor else None,
            None,
        )
        return IngressUnit(envelope, checkpoint, False)

    async def next_ingress(self) -> IngressUnit:
        if self._revoked:
            raise WhatsAppProviderError("WHATSAPP_RUNTIME_REVOKED", "runtime is revoked")
        return await self._ingress.get()

    async def accept_callback(self, request: VerifiedCallbackRequest) -> IngressUnit:
        del request
        raise WhatsAppProviderError(
            "WHATSAPP_CALLBACK_UNSUPPORTED", "linked-device transport has no HTTP callback"
        )

    async def commit_checkpoint(
        self, checkpoint: NativeCheckpoint, custody: CustodyReceipt
    ) -> None:
        response = await invoke_native(
            self.worker,
            "runtime.commit-checkpoint",
            {
                "kind": checkpoint.kind,
                "cursor": checkpoint.token,
                "journalId": custody.journal_id,
                "envelopeDigest": custody.envelope_digest,
            },
        )
        if response.get("committed") is not True:
            raise WhatsAppProviderError("WHATSAPP_CURSOR_NOT_COMMITTED", "worker refused cursor")

    async def hydrate(self, envelope: PlatformEventEnvelope) -> HydratedEvent:
        response = await invoke_native(
            self.worker,
            "runtime.hydrate",
            {"event": _json(envelope.payload), "digest": frozen_json_digest_v1(envelope.payload)},
        )
        event = response.get("event")
        if not isinstance(event, Mapping):
            raise WhatsAppProviderError("WHATSAPP_NATIVE_RESPONSE_INVALID", "hydrate event missing")
        return HydratedEvent(envelope, event)

    async def history(
        self, after: NativeCheckpoint | None, limit: int
    ) -> tuple[IngressUnit, ...]:
        if not 1 <= limit <= 256:
            raise ValueError("history limit must be between 1 and 256")
        response = await invoke_native(
            self.worker,
            "runtime.history",
            {"after": after.token if after else None, "limit": limit},
        )
        raw = response.get("units")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray, memoryview)):
            raise WhatsAppProviderError("WHATSAPP_NATIVE_RESPONSE_INVALID", "history units missing")
        units = []
        for item in raw:
            if not isinstance(item, Mapping) or not isinstance(item.get("event"), Mapping):
                raise WhatsAppProviderError("WHATSAPP_NATIVE_RESPONSE_INVALID", "invalid history unit")
            units.append(self._unit(dict(item["event"]), item.get("cursor")))
        return tuple(units)

    async def resolve(self, target: AuthorizedChatTarget) -> NativeTargetResolution:
        if target.chat_ref.instance != self.instance or target.chat_ref.account_scope != self.account_jid:
            return NativeTargetResolution(target, "unauthorized", None, None, "WHATSAPP_SCOPE_MISMATCH")
        response = await invoke_native(
            self.worker,
            "runtime.resolve",
            {"chatJid": target.chat_ref.native_chat_id},
        )
        return NativeTargetResolution(
            target,
            response.get("status"),
            response.get("chatJid"),
            None,
            response.get("reasonCode"),
        )

    def _assert_active(self) -> None:
        require_artifact(self.artifact)
        if self._revoked:
            raise WhatsAppProviderError("WHATSAPP_RUNTIME_REVOKED", "runtime is revoked")
        if self._blocked:
            raise WhatsAppProviderError("WHATSAPP_RUNTIME_BLOCKED", ",".join(sorted(self._blocked)))
        self.effect_fence.assert_allows(self.instance, self.account_jid)

    async def submit(self, operation) -> NativeSubmissionOutcome:
        self._assert_active()
        if operation.node_id in self._unknown:
            raise WhatsAppProviderError(
                "WHATSAPP_RECONCILE_REQUIRED", "unknown operation must not be resent"
            )
        response = await invoke_native(
            self.worker,
            "runtime.submit",
            {
                "nodeId": operation.node_id,
                "kind": operation.kind,
                "idempotencyKey": operation.idempotency_key,
                "sealedRequest": _json(operation.sealed_request),
                "instanceId": self.instance.instance_id,
                "generation": self.instance.generation,
                "accountJid": self.account_jid,
            },
        )
        outcome = _parts(response)
        if any(part.state == "unknown" for part in outcome.submissions):
            self._unknown.add(operation.node_id)
        return outcome

    async def reconcile(
        self, operation, prior: NativeSubmissionOutcome
    ) -> NativeSubmissionOutcome:
        self._assert_active()
        if not any(part.state == "unknown" for part in prior.submissions):
            raise WhatsAppProviderError(
                "WHATSAPP_RECONCILE_NOT_ALLOWED", "only original unknown operations reconcile"
            )
        response = await invoke_native(
            self.worker,
            "runtime.reconcile",
            {
                "nodeId": operation.node_id,
                "kind": operation.kind,
                "idempotencyKey": operation.idempotency_key,
                "sealedRequest": _json(operation.sealed_request),
                "instanceId": self.instance.instance_id,
                "generation": self.instance.generation,
                "accountJid": self.account_jid,
            },
        )
        outcome = _parts(response)
        if any(part.state == "unknown" for part in outcome.submissions):
            self._unknown.add(operation.node_id)
        else:
            self._unknown.discard(operation.node_id)
        return outcome

    async def reconnect(self, reason: str) -> None:
        self._assert_active()
        await invoke_native(
            self.worker,
            "runtime.reconnect",
            {
                "reason": reason,
                "instanceId": self.instance.instance_id,
                "generation": self.instance.generation,
                "accountJid": self.account_jid,
            },
        )

    async def health(self) -> AdapterHealth:
        reasons = set(self.artifact.reason_codes if not self.artifact.available else ()) | self._blocked
        if self._revoked:
            reasons.add("WHATSAPP_RUNTIME_REVOKED")
        state = "blocked" if reasons else "online"
        return AdapterHealth(
            state,
            "tryx:no-account-reviewed:v1",
            self.pending_ingress,
            len(self._unknown),
            tuple(sorted(reasons)),
        )

    async def close(self, deadline_ms: int) -> AdapterCloseOutcome:
        response = await invoke_native(
            self.worker, "runtime.close", {"deadlineMs": deadline_ms}
        )
        return AdapterCloseOutcome(
            response.get("state"),
            response.get("retainedIngress"),
            response.get("retainedDeliveries"),
            response.get("reasonCode"),
        )

    def revoke(self, reason: str) -> None:
        self._revoked = True
        self._blocked.add(reason)
        self.effect_fence.block(reason)
        try:
            self.worker.invoke(
                {
                    "operation": "runtime.revoke",
                    "reasonCode": reason,
                    "instanceId": self.instance.instance_id,
                    "generation": self.instance.generation,
                    "accountJid": self.account_jid,
                }
            )
        except Exception:
            # The local fence is immediate. A dead worker cannot perform an
            # effect; a live worker persists the same fence before acknowledging.
            pass


__all__ = ["WhatsAppRuntime"]
