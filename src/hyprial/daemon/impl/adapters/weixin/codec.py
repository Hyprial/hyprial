from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

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
    PartSubmission,
    PlatformRefsV1,
    PlatformScopeV1,
    PlatformUnknownV1,
    ReplyReference,
    SourceProvenance,
    TextBlock,
    WeixinProvenanceV1,
    canonical_json_v1,
    frozen_json_digest_v1,
)

from .errors import WeixinProtocolError

MAX_WEIXIN_PAYLOAD_BYTES = 256 * 1024
_SECRET_KEYS = ("authorization", "token", "secret", "cookie", "aeskey", "aes_key")


@dataclass(frozen=True, slots=True)
class DecodedWeixinEvent:
    aliases: tuple[NativeAlias, ...]
    provenance: WeixinProvenanceV1
    text: str
    sender_id: str
    chat_id: str
    item_types: tuple[str, ...]


def normalize_weixin_api_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise WeixinProtocolError("invalid-base-url") from exc
    host = (parsed.hostname or "").rstrip(".").lower()
    trusted = host in {"weixin.qq.com", "wechat.com"} or host.endswith(
        (".weixin.qq.com", ".wechat.com")
    )
    if (
        parsed.scheme != "https"
        or not trusted
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise WeixinProtocolError("untrusted-base-url")
    path = parsed.path or "/"
    if not path.endswith("/"):
        path += "/"
    return urlunsplit(("https", host, path, "", ""))


def _bounded_mapping(payload: Mapping[str, object]) -> dict[str, object]:
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WeixinProtocolError("invalid-payload") from exc
    if len(encoded) > MAX_WEIXIN_PAYLOAD_BYTES:
        raise WeixinProtocolError("payload-too-large")
    return json.loads(encoded)


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _item_type(item: Mapping[str, object]) -> str:
    value = item.get("type")
    named = {1: "text", 2: "image", 3: "voice", 4: "file", 5: "reply"}
    if isinstance(value, int):
        return named.get(value, f"native-{value}")
    return _text(value) or "unknown"


def _secret_paths(value: object, path: str = "$") -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            child = f"{path}.{key}"
            if any(part in str(key).lower() for part in _SECRET_KEYS):
                found.append(child)
            else:
                found.extend(_secret_paths(nested, child))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(_secret_paths(nested, f"{path}[{index}]"))
    return tuple(found)


def decode_weixin_event(
    instance: AdapterInstanceRef,
    payload: Mapping[str, object],
    *,
    raw_source_ref: str,
) -> DecodedWeixinEvent:
    if instance.kind != "weixin" or instance.variant != "personal":
        raise WeixinProtocolError("variant-mismatch")
    body = _bounded_mapping(payload)
    message_id = _text(body.get("message_id")) or _text(body.get("msg_id"))
    sender_id = _text(body.get("from_user_id"))
    recipient = _text(body.get("to_user_id"))
    if not message_id or not sender_id or not recipient:
        raise WeixinProtocolError("missing-stable-identity")
    event_id = _text(body.get("event_id"))
    seq = body.get("seq")
    account_scope = instance.external_fingerprint
    aliases: list[NativeAlias] = []
    if event_id:
        aliases.append(
            NativeAlias(
                instance.instance_id,
                instance.generation,
                account_scope,
                "event",
                "create",
                "event",
                event_id,
                "s:0",
            )
        )
    aliases.append(
        NativeAlias(
            instance.instance_id,
            instance.generation,
            account_scope,
            "message",
            "create",
            "message",
            message_id,
            "s:0",
        )
    )
    if isinstance(seq, (str, int)) and not isinstance(seq, bool):
        aliases.append(
            NativeAlias(
                instance.instance_id,
                instance.generation,
                account_scope,
                "update-sequence",
                "create",
                "cursor-item",
                str(seq),
                "i:0",
            )
        )
    items = body.get("item_list")
    if not isinstance(items, list) or len(items) > 128:
        raise WeixinProtocolError("invalid-item-list")
    item_maps = [item for item in items if isinstance(item, dict)]
    item_types = tuple(_item_type(item) for item in item_maps)
    texts: list[str] = []
    reply_message_id: str | None = None
    media_refs: list[str] = []
    for index, item in enumerate(item_maps):
        text_item = item.get("text_item")
        voice_item = item.get("voice_item")
        if isinstance(text_item, dict) and _text(text_item.get("text")):
            texts.append(_text(text_item.get("text")) or "")
        elif isinstance(voice_item, dict) and _text(voice_item.get("text")):
            texts.append(_text(voice_item.get("text")) or "")
        reply = item.get("ref_msg") or item.get("reply_item")
        if isinstance(reply, dict):
            reply_message_id = _text(reply.get("message_id")) or reply_message_id
        if _item_type(item) in {"image", "file", "voice"}:
            media_refs.append(f"sealed-media:{message_id}:{index}")
    context_token = _text(body.get("context_token"))
    context_ref = (
        "sealed-context:"
        + hashlib.sha256(context_token.encode("utf-8")).hexdigest()
        if context_token
        else None
    )
    safe_unknown = {
        key: value
        for key, value in body.items()
        if key in {"message_type", "message_state", "create_time_ms", "seq", "run_id"}
    }
    if _secret_paths(safe_unknown):
        raise WeixinProtocolError("secret-field")
    provenance = WeixinProvenanceV1(
        "hyprial.chat.platform.weixin/v1",
        PlatformScopeV1(
            instance.organization_id,
            instance.instance_id,
            instance.generation,
            account_scope,
            "weixin-ilink",
        ),
        PlatformRefsV1(
            event_id=event_id,
            message_id=message_id,
            reply_id=reply_message_id,
            attachment_refs=tuple(media_refs),
        ),
        PlatformUnknownV1(safe_unknown, raw_source_ref),
        sender_id,
        sender_id,
        ",".join(item_types) or "unknown",
        context_ref,
        reply_message_id,
        tuple(media_refs),
    )
    return DecodedWeixinEvent(
        tuple(aliases), provenance, "\n".join(texts), sender_id, sender_id, item_types
    )


def classify_weixin_submission(
    result: object, *, operation_started: bool, part_index: int = 0
) -> PartSubmission:
    if isinstance(result, BaseException):
        if operation_started:
            return PartSubmission(part_index, "unknown", None, False, "weixin-submit-unknown")
        return PartSubmission(
            part_index, "failed-retryable", None, True, "weixin-submit-not-started"
        )
    if not isinstance(result, Mapping):
        return PartSubmission(part_index, "unknown", None, False, "weixin-invalid-response")
    code = result.get("ret", result.get("errcode", 0))
    if code not in (0, "0", None):
        return PartSubmission(
            part_index,
            "failed-final",
            None,
            not operation_started,
            f"weixin-provider-{str(code)[:160]}",
        )
    message_id = _text(result.get("message_id"))
    ids = result.get("providerMessageIds")
    if not message_id and isinstance(ids, list) and ids:
        message_id = _text(ids[0])
    if not message_id:
        return PartSubmission(part_index, "unknown", None, False, "weixin-missing-message-id")
    return PartSubmission(part_index, "accepted", message_id, False, None)


__all__ = [
    "DecodedWeixinEvent",
    "MAX_WEIXIN_PAYLOAD_BYTES",
    "classify_weixin_submission",
    "decode_weixin_event",
    "normalize_weixin_api_base_url",
]


def sanitize_weixin_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Remove receiver-owned context/media secrets before daemon journaling."""

    body = _bounded_mapping(payload)
    token = body.pop("context_token", None)
    if isinstance(token, str) and token:
        body["context_token_ref"] = "sealed-context:" + hashlib.sha256(token.encode()).hexdigest()
    items = body.get("item_list")
    if isinstance(items, list):
        for index, raw in enumerate(items):
            if not isinstance(raw, dict):
                continue
            for field in ("image_item", "file_item", "voice_item"):
                item = raw.get(field)
                if not isinstance(item, dict):
                    continue
                for key in ("aeskey", "aes_key"):
                    value = item.pop(key, None)
                    if isinstance(value, str) and value:
                        item[f"{key}_ref"] = f"sealed-media-key:{index}"
                media = item.get("media")
                if isinstance(media, dict):
                    for key in ("aeskey", "aes_key", "encrypt_query_param"):
                        if media.pop(key, None) is not None:
                            media[f"{key}_ref"] = f"sealed-media:{index}"
    return body


__all__.append("sanitize_weixin_payload")


def _message_text(message: object) -> str:
    blocks = getattr(message, "content", ())
    text = "\n".join(
        block.text for block in blocks if isinstance(block, TextBlock) and block.text
    )
    if not text:
        raise WeixinProtocolError("text-content-required")
    if len(text.encode("utf-8")) > 32 * 1024:
        raise WeixinProtocolError("text-too-large")
    return text


class WeixinCodec:
    """Pure iLink codec over frozen envelopes and authorized native targets."""

    def __init__(self, instance: AdapterInstanceRef) -> None:
        if instance.kind != "weixin" or instance.variant != "personal":
            raise WeixinProtocolError("variant-mismatch")
        self._instance = instance

    def normalize(self, event: HydratedEvent) -> NormalizedIngress:
        body = json.loads(canonical_json_v1(event.body))
        if not isinstance(body, dict):
            raise WeixinProtocolError("invalid-payload")
        decoded = decode_weixin_event(
            self._instance,
            body,
            raw_source_ref=event.envelope.observation.raw_safe_ref,
        )
        observation = event.envelope.observation
        provenance = SourceProvenance(
            f"weixin-ingress:{observation.observation_digest}",
            (observation.observation_id,),
            decoded.aliases,
            observation.received_at_ms,
            None,
            "native-human",
            decoded.sender_id,
        )
        message_id = decoded.provenance.refs.message_id or "unknown"
        attachments = tuple(
            AttachmentHandle(ref, "other", None, None, None, None, message_id, None)
            for ref in decoded.provenance.media_refs
        )
        reply = (
            ReplyReference(None, decoded.provenance.reply_message_id, None, (), (), None)
            if decoded.provenance.reply_message_id
            else None
        )
        content = (TextBlock("text", decoded.text),) if decoded.text else ()
        return NormalizedIngress(
            {
                "platform": "weixin",
                "chatId": decoded.chat_id,
                "senderId": decoded.sender_id,
                "platformProvenance": decoded.provenance,
            },
            provenance,
            content,
            attachments,
            reply,
        )

    def render(
        self, target: NativeTargetResolution, message: ChatMessage | IntentV1
    ) -> OperationDraft:
        if target.status != "resolved" or not target.native_chat_id:
            raise WeixinProtocolError("target-unresolved")
        text = _message_text(message)
        template = {
            "toUserId": target.native_chat_id,
            "text": text,
            "replyTo": target.target.canonical_parent_id,
        }
        digest = frozen_json_digest_v1(template)
        return OperationDraft(
            "weixin-ilink-renderer/v1",
            "weixin-ilink/authenticated",
            digest,
            (OperationDraftNode("send", "text-send", (), template),),
        )


__all__.append("WeixinCodec")
