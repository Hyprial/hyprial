from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    AttachmentHandle,
    ChatMessage,
    DingTalkProvenanceV1,
    HydratedEvent,
    IntentV1,
    NativeAlias,
    NativeTargetResolution,
    NormalizedIngress,
    OperationDraft,
    OperationDraftNode,
    PartSubmission,
    PlatformRefsV1,
    PlatformScopeV1,
    PlatformUnknownV1,
    ReplyReference,
    SourceProvenance,
    TextBlock,
    canonical_json_v1,
    frozen_json_digest_v1,
)

from .errors import DingTalkProtocolError

MAX_DINGTALK_PAYLOAD_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class DecodedDingTalkEvent:
    aliases: tuple[NativeAlias, ...]
    provenance: DingTalkProvenanceV1
    text: str
    sender_id: str
    chat_kind: str
    addressed: bool


class DingTalkReplyRouteStore:
    """Short-lived route projection over caller-supplied durable state."""

    def __init__(
        self,
        *,
        now_ms: object | None = None,
        ttl_ms: int = 3_000_000,
        state: dict[str, object] | None = None,
    ) -> None:
        self._now_ms = now_ms if callable(now_ms) else lambda: int(time.time() * 1000)
        self._ttl_ms = min(max(ttl_ms, 1), 3_600_000)
        self._routes = state if state is not None else {}

    def set_now_ms(self, value: object) -> None:
        if not callable(value):
            raise TypeError("now_ms must be callable")
        self._now_ms = value

    def seal(
        self,
        url: str,
        *,
        instance_id: str,
        generation: int,
        conversation_id: str,
    ) -> str:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or host not in {"oapi.dingtalk.com", "api.dingtalk.com"}
            or parsed.port not in (None, 443)
            or parsed.username is not None
        ):
            raise DingTalkProtocolError("untrusted-session-webhook")
        route_ref = f"dingtalk-session:{uuid.uuid4().hex}"
        self._routes[route_ref] = {
            "instanceId": instance_id,
            "generation": generation,
            "conversationId": conversation_id,
            "url": url,
            "expiresAtMs": self._now_ms() + self._ttl_ms,
        }
        return route_ref

    def resolve(self, route_ref: str, *, instance_id: str, generation: int) -> str:
        route = self._routes.get(route_ref)
        if not isinstance(route, Mapping):
            raise DingTalkProtocolError("reply-route-missing")
        if route.get("instanceId") != instance_id or route.get("generation") != generation:
            raise DingTalkProtocolError("reply-route-generation-mismatch")
        expires_at_ms = route.get("expiresAtMs")
        url = route.get("url")
        if not isinstance(expires_at_ms, int) or not isinstance(url, str):
            raise DingTalkProtocolError("reply-route-invalid")
        if self._now_ms() >= expires_at_ms:
            del self._routes[route_ref]
            raise DingTalkProtocolError("reply-route-expired")
        return url

    def resolve_ref(
        self, native_chat_id: str, *, instance_id: str, generation: int
    ) -> str:
        candidates: list[tuple[int, str]] = []
        for route_ref, route in tuple(self._routes.items()):
            if not isinstance(route_ref, str) or not isinstance(route, Mapping):
                continue
            if (
                route.get("instanceId") != instance_id
                or route.get("generation") != generation
                or route.get("conversationId") != native_chat_id
            ):
                continue
            expires_at_ms = route.get("expiresAtMs")
            if not isinstance(expires_at_ms, int):
                continue
            if self._now_ms() >= expires_at_ms:
                del self._routes[route_ref]
                continue
            candidates.append((expires_at_ms, route_ref))
        if not candidates:
            raise DingTalkProtocolError("reply-route-missing")
        return max(candidates)[1]


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _bounded(payload: Mapping[str, object]) -> dict[str, object]:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise DingTalkProtocolError("invalid-payload") from exc
    if len(encoded) > MAX_DINGTALK_PAYLOAD_BYTES:
        raise DingTalkProtocolError("payload-too-large")
    return json.loads(encoded)


def decode_dingtalk_message(
    instance: AdapterInstanceRef,
    payload: Mapping[str, object],
    *,
    routes: DingTalkReplyRouteStore,
    raw_source_ref: str,
) -> DecodedDingTalkEvent:
    if instance.kind != "dingtalk" or instance.variant != "stream":
        raise DingTalkProtocolError("variant-mismatch")
    body = _bounded(payload)
    message_id = _text(body.get("msgId"))
    event_id = _text(body.get("eventId"))
    conversation_id = _text(body.get("conversationId"))
    sender = _text(body.get("senderStaffId")) or _text(body.get("senderId"))
    conversation_type = str(body.get("conversationType", ""))
    if not (message_id or event_id) or not conversation_id or not sender:
        raise DingTalkProtocolError("missing-stable-identity")
    route_ref: str | None = None
    webhook = _text(body.pop("sessionWebhook", None))
    if webhook:
        route_ref = routes.seal(
            webhook,
            instance_id=instance.instance_id,
            generation=instance.generation,
            conversation_id=conversation_id,
        )
    text_body = body.get("text")
    text = _text(text_body.get("content")) if isinstance(text_body, dict) else None
    quote_id: str | None = None
    if isinstance(text_body, dict) and text_body.get("isReplyMsg") is True:
        replied = text_body.get("repliedMsg")
        if isinstance(replied, dict):
            quote_id = _text(replied.get("msgId")) or _text(replied.get("messageId"))
    aliases: list[NativeAlias] = []
    for resource, alias_kind, value in (
        ("event", "event", event_id),
        ("message", "message", message_id),
    ):
        if value:
            aliases.append(
                NativeAlias(
                    instance.instance_id,
                    instance.generation,
                    instance.external_fingerprint,
                    resource,
                    "create",
                    alias_kind,  # type: ignore[arg-type]
                    value,
                    "s:0",
                )
            )
    media_refs: list[str] = []
    message_type = _text(body.get("msgtype")) or "unknown"
    if message_type.lower() in {"picture", "file", "audio", "voice", "video", "richtext"}:
        media_refs.append(f"sealed-media:{message_id or event_id}:0")
    safe_unknown = {
        key: body[key]
        for key in ("conversationTitle", "chatbotCorpId", "createAt", "msgtype", "isInAtList")
        if key in body
    }
    provenance = DingTalkProvenanceV1(
        "hyprial.chat.platform.dingtalk/v1",
        PlatformScopeV1(
            instance.organization_id,
            instance.instance_id,
            instance.generation,
            instance.external_fingerprint,
            "dingtalk-stream",
        ),
        PlatformRefsV1(
            event_id=event_id,
            message_id=message_id,
            reply_id=quote_id,
            attachment_refs=tuple(media_refs),
        ),
        PlatformUnknownV1(safe_unknown, raw_source_ref),
        conversation_id,
        conversation_type,
        sender,
        route_ref,
        quote_id,
        None,
        tuple(media_refs),
    )
    return DecodedDingTalkEvent(
        tuple(aliases),
        provenance,
        text or "",
        sender,
        "group" if conversation_type == "2" else "dm",
        conversation_type != "2" or body.get("isInAtList") is True,
    )


def classify_dingtalk_card_step(
    *,
    step: str,
    result: object,
    operation_started: bool,
    out_track_id: str | None = None,
    part_index: int = 0,
) -> PartSubmission:
    if isinstance(result, BaseException):
        if operation_started:
            suffix = f":{out_track_id}" if out_track_id else ""
            return PartSubmission(
                part_index,
                "unknown",
                None,
                False,
                f"dingtalk-card-{step}-unknown{suffix}",
            )
        return PartSubmission(
            part_index, "failed-retryable", None, True, f"dingtalk-card-{step}-not-started"
        )
    if not isinstance(result, Mapping):
        return PartSubmission(
            part_index, "unknown", None, False, f"dingtalk-card-{step}-invalid-response"
        )
    code = result.get("errcode", result.get("code", 0))
    if code not in (0, "0", None):
        return PartSubmission(
            part_index,
            "failed-final",
            None,
            not operation_started,
            f"dingtalk-provider-{str(code)[:160]}",
        )
    provider_id = _text(result.get("outTrackId")) or _text(result.get("messageId"))
    if not provider_id:
        return PartSubmission(
            part_index, "unknown", None, False, f"dingtalk-card-{step}-missing-id"
        )
    return PartSubmission(part_index, "accepted", provider_id, False, None)


def payload_digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


__all__ = [
    "DecodedDingTalkEvent",
    "DingTalkReplyRouteStore",
    "classify_dingtalk_card_step",
    "decode_dingtalk_message",
    "payload_digest",
]


def _message_text(message: object) -> str:
    text = "\n".join(
        block.text
        for block in getattr(message, "content", ())
        if isinstance(block, TextBlock) and block.text
    )
    if not text:
        raise DingTalkProtocolError("text-content-required")
    if len(text.encode("utf-8")) > 20_000:
        raise DingTalkProtocolError("text-too-large")
    return text


class DingTalkCodec:
    def __init__(
        self, instance: AdapterInstanceRef, *, routes: DingTalkReplyRouteStore
    ) -> None:
        if instance.kind != "dingtalk" or instance.variant != "stream":
            raise DingTalkProtocolError("variant-mismatch")
        self._instance = instance
        self._routes = routes

    def normalize(self, event: HydratedEvent) -> NormalizedIngress:
        body = json.loads(canonical_json_v1(event.body))
        if not isinstance(body, dict):
            raise DingTalkProtocolError("invalid-payload")
        decoded = decode_dingtalk_message(
            self._instance,
            body,
            routes=self._routes,
            raw_source_ref=event.envelope.observation.raw_safe_ref,
        )
        observation = event.envelope.observation
        provenance = SourceProvenance(
            f"dingtalk-ingress:{observation.observation_digest}",
            (observation.observation_id,),
            decoded.aliases,
            observation.received_at_ms,
            None,
            "native-human",
            decoded.sender_id,
        )
        message_id = decoded.provenance.refs.message_id or decoded.provenance.refs.event_id or "unknown"
        attachments = tuple(
            AttachmentHandle(ref, "other", None, None, None, None, message_id, None)
            for ref in decoded.provenance.media_refs
        )
        reply = (
            ReplyReference(None, decoded.provenance.quote_message_id, None, (), (), None)
            if decoded.provenance.quote_message_id
            else None
        )
        return NormalizedIngress(
            {
                "platform": "dingtalk",
                "conversationId": decoded.provenance.conversation_id,
                "senderId": decoded.sender_id,
                "sessionWebhookRef": body.get("sessionWebhookRef"),
                "platformProvenance": decoded.provenance,
            },
            provenance,
            (TextBlock("text", decoded.text),) if decoded.text else (),
            attachments,
            reply,
        )

    def render(
        self, target: NativeTargetResolution, message: ChatMessage | IntentV1
    ) -> OperationDraft:
        if target.status != "resolved" or not target.native_chat_id:
            raise DingTalkProtocolError("target-unresolved")
        template = {
            "conversationId": target.native_chat_id,
            "sessionWebhookRef": target.native_thread_id,
            "text": _message_text(message),
            "replyTo": target.target.canonical_parent_id,
        }
        return OperationDraft(
            "dingtalk-stream-renderer/v1",
            "dingtalk-stream/authenticated",
            frozen_json_digest_v1(template),
            (OperationDraftNode("reply", "text-send", (), template),),
        )


__all__.append("DingTalkCodec")
