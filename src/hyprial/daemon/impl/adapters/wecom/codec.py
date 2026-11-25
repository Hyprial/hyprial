from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass

from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    AttachmentHandle,
    ChatMessage,
    HydratedEvent,
    IntentV1,
    NativeAlias,
    NativeTargetResolution,
    NormalizedIngress,
    OperationDraft,
    OperationDraftNode,
    PlatformRefsV1,
    PlatformScopeV1,
    PlatformUnknownV1,
    SourceProvenance,
    TextBlock,
    WeComBotProvenanceV1,
    WeComAppProvenanceV1,
    canonical_json_v1,
    frozen_json_digest_v1,
)

from .errors import WeComProtocolError

MAX_WECOM_PAYLOAD_BYTES = 1024 * 1024
_SECRET_PARTS = ("secret", "token", "authorization", "cookie", "aeskey", "aes_key")


@dataclass(frozen=True, slots=True)
class DecodedWeComBotEvent:
    aliases: tuple[NativeAlias, ...]
    provenance: WeComBotProvenanceV1
    text: str
    chat_kind: str
    sequence: int


@dataclass(frozen=True, slots=True)
class DecodedWeComApplicationEvent:
    aliases: tuple[NativeAlias, ...]
    provenance: WeComAppProvenanceV1
    text: str
    sender_id: str
    media_refs: tuple[str, ...]


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _secret_paths(value: object, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            child = f"{path}.{key}"
            if any(part in str(key).lower() for part in _SECRET_PARTS):
                found.append(child)
            else:
                found.extend(_secret_paths(nested, child))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(_secret_paths(nested, f"{path}[{index}]"))
    return found


def decode_wecom_bot_frame(
    instance: AdapterInstanceRef,
    frame: Mapping[str, object],
    *,
    raw_source_ref: str,
) -> DecodedWeComBotEvent:
    if instance.kind != "wecom" or instance.variant != "bot":
        raise WeComProtocolError("variant-mismatch")
    try:
        encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise WeComProtocolError("invalid-frame") from exc
    if len(encoded) > MAX_WECOM_PAYLOAD_BYTES:
        raise WeComProtocolError("payload-too-large")
    copied = json.loads(encoded)
    if _secret_paths(copied):
        raise WeComProtocolError("secret-field-in-frame")
    headers = copied.get("headers")
    req_id = _text(headers.get("req_id")) if isinstance(headers, dict) else None
    body = copied.get("body")
    if not req_id or not isinstance(body, dict):
        raise WeComProtocolError("invalid-frame")
    message_id = _text(body.get("msgid"))
    chat_id = _text(body.get("chatid"))
    sender = body.get("from")
    sender_id = _text(sender.get("userid")) if isinstance(sender, dict) else None
    if not message_id or not chat_id or not sender_id:
        raise WeComProtocolError("missing-stable-identity")
    chat_type = _text(body.get("chattype")) or "unknown"
    msg_type = _text(body.get("msgtype")) or "unknown"
    content = body.get("text")
    text = _text(content.get("content")) if isinstance(content, dict) else ""
    quote = body.get("quote")
    quote_id = _text(quote.get("msgid")) if isinstance(quote, dict) else None
    media_refs = (
        (f"sealed-media:{message_id}:0",)
        if msg_type in {"image", "mixed", "voice", "file"}
        else ()
    )
    aliases = (
        NativeAlias(
            instance.instance_id,
            instance.generation,
            instance.external_fingerprint,
            "request",
            "create",
            "event",
            req_id,
            "s:0",
        ),
        NativeAlias(
            instance.instance_id,
            instance.generation,
            instance.external_fingerprint,
            "message",
            "create",
            "message",
            message_id,
            "s:0",
        ),
    )
    provenance = WeComBotProvenanceV1(
        "hyprial.chat.platform.wecom-bot/v1",
        PlatformScopeV1(
            instance.organization_id,
            instance.instance_id,
            instance.generation,
            instance.external_fingerprint,
            "wecom-aibot-websocket",
        ),
        PlatformRefsV1(
            event_id=req_id,
            message_id=message_id,
            reply_id=quote_id,
            attachment_refs=media_refs,
        ),
        PlatformUnknownV1(
            {"cmd": copied.get("cmd", ""), "msgtype": msg_type},
            raw_source_ref,
        ),
        instance.external_fingerprint,
        req_id,
        chat_id,
        chat_type,
        sender_id,
        _text(copied.get("cmd")) or "unknown",
        quote_id,
        media_refs,
    )
    return DecodedWeComBotEvent(
        aliases,
        provenance,
        text or "",
        "dm" if chat_type in {"single", "direct", "1"} else "group",
        0,
    )


def frame_digest(frame: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(frame, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def sanitize_wecom_bot_frame(frame: Mapping[str, object]) -> dict[str, object]:
    copied = json.loads(json.dumps(frame, ensure_ascii=False))
    body = copied.get("body")
    if isinstance(body, dict):
        for container_name in ("image", "file", "voice", "mixed"):
            container = body.get(container_name)
            if not isinstance(container, dict):
                continue
            for key in ("aeskey", "aes_key", "url"):
                if container.pop(key, None) is not None:
                    container[f"{key}_ref"] = f"sealed-wecom-media:{container_name}"
    return copied


def decode_wecom_application_plaintext(
    instance: AdapterInstanceRef,
    plaintext: bytes,
    *,
    corp_id: str,
    agent_id: str,
    callback_proof_ref: str,
    raw_source_ref: str,
) -> DecodedWeComApplicationEvent:
    if instance.kind != "wecom" or instance.variant != "application":
        raise WeComProtocolError("variant-mismatch")
    if len(plaintext) > MAX_WECOM_PAYLOAD_BYTES:
        raise WeComProtocolError("payload-too-large")
    try:
        root = ET.fromstring(plaintext)
    except ET.ParseError as exc:
        raise WeComProtocolError("callback-plaintext-invalid") from exc
    fields = {child.tag: child.text or "" for child in root}
    sender = _text(fields.get("FromUserName"))
    message_id = _text(fields.get("MsgId"))
    message_type = (_text(fields.get("MsgType")) or "unknown").lower()
    if not sender or not message_id:
        raise WeComProtocolError("missing-stable-identity")
    media_id = _text(fields.get("MediaId"))
    media_refs = (f"sealed-wecom-app-media:{message_id}:0",) if media_id else ()
    aliases = (
        NativeAlias(
            instance.instance_id,
            instance.generation,
            instance.external_fingerprint,
            "message",
            "create",
            "message",
            message_id,
            "s:0",
        ),
    )
    provenance = WeComAppProvenanceV1(
        "hyprial.chat.platform.wecom-app/v1",
        PlatformScopeV1(
            instance.organization_id,
            instance.instance_id,
            instance.generation,
            instance.external_fingerprint,
            "wecom-application-callback",
        ),
        PlatformRefsV1(message_id=message_id, attachment_refs=media_refs),
        PlatformUnknownV1(
            {"createTime": fields.get("CreateTime", ""), "event": fields.get("Event", "")},
            raw_source_ref,
        ),
        corp_id,
        agent_id,
        sender,
        message_id,
        message_type,
        callback_proof_ref,
        media_refs,
    )
    return DecodedWeComApplicationEvent(
        aliases, provenance, fields.get("Content", ""), sender, media_refs
    )


__all__ = [
    "DecodedWeComApplicationEvent",
    "DecodedWeComBotEvent",
    "decode_wecom_application_plaintext",
    "decode_wecom_bot_frame",
    "frame_digest",
    "sanitize_wecom_bot_frame",
]


def _message_text(message: object, *, max_bytes: int) -> str:
    text = "\n".join(
        block.text
        for block in getattr(message, "content", ())
        if isinstance(block, TextBlock) and block.text
    )
    if not text:
        raise WeComProtocolError("text-content-required")
    if len(text.encode("utf-8")) > max_bytes:
        raise WeComProtocolError("text-too-large")
    return text


class WeComBotCodec:
    def __init__(self, instance: AdapterInstanceRef) -> None:
        if instance.kind != "wecom" or instance.variant != "bot":
            raise WeComProtocolError("variant-mismatch")
        self._instance = instance

    def normalize(self, event: HydratedEvent) -> NormalizedIngress:
        body = json.loads(canonical_json_v1(event.body))
        if not isinstance(body, dict):
            raise WeComProtocolError("invalid-frame")
        decoded = decode_wecom_bot_frame(
            self._instance, body, raw_source_ref=event.envelope.observation.raw_safe_ref
        )
        observation = event.envelope.observation
        source = SourceProvenance(
            f"wecom-bot-ingress:{observation.observation_digest}",
            (observation.observation_id,),
            decoded.aliases,
            observation.received_at_ms,
            None,
            "native-human",
            decoded.provenance.sender_id,
        )
        message_id = decoded.provenance.refs.message_id or "unknown"
        attachments = tuple(
            AttachmentHandle(ref, "other", None, None, None, None, message_id, None)
            for ref in decoded.provenance.media_refs
        )
        return NormalizedIngress(
            {
                "platform": "wecom-bot",
                "chatId": decoded.provenance.chat_id,
                "reqId": decoded.provenance.req_id,
                "platformProvenance": decoded.provenance,
            },
            source,
            (TextBlock("text", decoded.text),) if decoded.text else (),
            attachments,
            None,
        )

    def render(
        self, target: NativeTargetResolution, message: ChatMessage | IntentV1
    ) -> OperationDraft:
        if target.status != "resolved" or not target.native_chat_id:
            raise WeComProtocolError("target-unresolved")
        template = {
            "chatId": target.native_chat_id,
            "replyReqId": target.target.canonical_parent_id,
            "body": {"msgtype": "text", "text": {"content": _message_text(message, max_bytes=20_000)}},
        }
        return OperationDraft(
            "wecom-bot-renderer/v1",
            "wecom-bot/authenticated",
            frozen_json_digest_v1(template),
            (OperationDraftNode("send", "text-send", (), template),),
        )


class WeComApplicationCodec:
    def __init__(self, instance: AdapterInstanceRef) -> None:
        if instance.kind != "wecom" or instance.variant != "application":
            raise WeComProtocolError("variant-mismatch")
        self._instance = instance

    def normalize(self, event: HydratedEvent) -> NormalizedIngress:
        body = json.loads(canonical_json_v1(event.body))
        if not isinstance(body, dict):
            raise WeComProtocolError("callback-plaintext-invalid")
        sender = _text(body.get("fromUser"))
        message_id = _text(body.get("msgId"))
        if not sender or not message_id:
            raise WeComProtocolError("missing-stable-identity")
        observation = event.envelope.observation
        source = SourceProvenance(
            f"wecom-app-ingress:{observation.observation_digest}",
            (observation.observation_id,),
            observation.aliases,
            observation.received_at_ms,
            None,
            "native-human",
            sender,
        )
        media_refs = body.get("mediaRefs") if isinstance(body.get("mediaRefs"), list) else []
        attachments = tuple(
            AttachmentHandle(str(ref), "other", None, None, None, None, message_id, None)
            for ref in media_refs
        )
        text = _text(body.get("text"))
        return NormalizedIngress(
            {"platform": "wecom-application", "fromUser": sender, "msgId": message_id},
            source,
            (TextBlock("text", text),) if text else (),
            attachments,
            None,
        )

    def render(
        self, target: NativeTargetResolution, message: ChatMessage | IntentV1
    ) -> OperationDraft:
        if target.status != "resolved" or not target.native_chat_id:
            raise WeComProtocolError("target-unresolved")
        template = {
            "toUserId": target.native_chat_id,
            "text": _message_text(message, max_bytes=2048),
        }
        return OperationDraft(
            "wecom-application-renderer/v1",
            "wecom-application/authenticated",
            frozen_json_digest_v1(template),
            (OperationDraftNode("send", "text-send", (), template),),
        )


__all__.extend(["WeComApplicationCodec", "WeComBotCodec"])
