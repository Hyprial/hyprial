"""WhatsApp linked-device normalization and presentation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from hyprial.daemon.impl.adapters.contracts import (
    AttachmentBlock,
    AttachmentHandle,
    AudienceSnapshot,
    ChatContext,
    ChatMessage,
    ChatRef,
    FrozenJsonMap,
    HydratedEvent,
    NativeTargetResolution,
    NormalizedIngress,
    OperationDraft,
    OperationDraftNode,
    PlatformRefsV1,
    PlatformScopeV1,
    PlatformUnknownV1,
    PrincipalRef,
    ReplyReference,
    SourceProvenance,
    TextBlock,
    UnsupportedNativeBlock,
    WhatsAppProvenanceV1,
    canonical_json_v1,
    freeze_json_v1,
    frozen_json_digest_v1,
)

from .boundary import WhatsAppProviderError


_EVENT_KEYS = frozenset(
    {
        "schema",
        "accountJid",
        "chatJid",
        "participantJid",
        "senderJid",
        "messageId",
        "deviceId",
        "quotedMessageId",
        "wrappers",
        "viewOnce",
        "isFromMe",
        "isGroup",
        "eventKind",
        "text",
        "pushName",
        "providerCreatedAtMs",
        "receivedAtMs",
        "media",
        "unknown",
    }
)
_EVENT_KINDS = frozenset({"message", "edit", "reaction", "receipt"})
_MEDIA_KINDS = frozenset({"image", "file", "audio", "video", "other"})


@dataclass(frozen=True, slots=True)
class WhatsAppNormalizedIngress(NormalizedIngress):
    platform: WhatsAppProvenanceV1


def _thaw(value: object) -> object:
    if isinstance(value, FrozenJsonMap):
        return json.loads(canonical_json_v1(value))
    return value


def _text(value: object, name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value or len(value.encode()) > 4096:
        raise ValueError(f"{name} must be a bounded non-empty string")
    return value


def _sequence(value: object, name: str, *, limit: int = 128) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray, memoryview)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be an array")
    result = tuple(value)
    if len(result) > limit:
        raise ValueError(f"{name} exceeds {limit} entries")
    return result


def _event_map(event: HydratedEvent) -> dict[str, object]:
    value = _thaw(event.body)
    if not isinstance(value, Mapping) or set(value) != _EVENT_KEYS:
        raise WhatsAppProviderError("WHATSAPP_EVENT_INVALID", "native event shape is not closed")
    return dict(value)


class WhatsAppCodec:
    def __init__(self, instance, *, account_jid: str) -> None:
        if instance.kind != "whatsapp" or instance.variant != "linked-device":
            raise ValueError("WhatsApp codec requires a linked-device instance")
        self.instance = instance
        self.account_jid = _text(account_jid, "account_jid")

    def normalize(self, event: HydratedEvent) -> NormalizedIngress:
        try:
            payload = _event_map(event)
            account_jid = _text(payload["accountJid"], "accountJid")
            if account_jid != self.account_jid:
                raise WhatsAppProviderError(
                    "WHATSAPP_ACCOUNT_MISMATCH", "native event belongs to another account"
                )
            chat_jid = _text(payload["chatJid"], "chatJid")
            sender_jid = _text(payload["senderJid"], "senderJid")
            message_id = _text(payload["messageId"], "messageId")
            participant_jid = _text(payload["participantJid"], "participantJid", optional=True)
            device_id = _text(payload["deviceId"], "deviceId", optional=True)
            quoted_id = _text(payload["quotedMessageId"], "quotedMessageId", optional=True)
            wrappers = tuple(_text(item, "wrapper") for item in _sequence(payload["wrappers"], "wrappers"))
            if type(payload["viewOnce"]) is not bool or type(payload["isFromMe"]) is not bool:
                raise TypeError("viewOnce/isFromMe must be bool")
            if type(payload["isGroup"]) is not bool or payload["eventKind"] not in _EVENT_KINDS:
                raise ValueError("invalid chat/event kind")
            unknown = PlatformUnknownV1(
                fields=payload["unknown"],
                raw_source_ref=event.envelope.observation.raw_safe_ref,
            )
            attachments = self._attachments(payload["media"], message_id)
        except WhatsAppProviderError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise WhatsAppProviderError("WHATSAPP_EVENT_INVALID", str(error)) from error

        is_from_me = payload["isFromMe"]
        sender = PrincipalRef(
            provider_subject_id=sender_jid,
            open_id=None,
            union_id=None,
            display_name=payload["pushName"] if isinstance(payload["pushName"], str) else None,
            hyprial_principal=None,
            resolution="unresolved",
            proof_ref=None,
            is_self=is_from_me,
            standing=None,
            source="whatsapp-linked-device",
        )
        chat_ref = ChatRef(
            ref_id=f"chat-ref:v1:whatsapp:{self.instance.instance_id}:{chat_jid}",
            instance=self.instance,
            account_scope=account_jid,
            native_chat_id=chat_jid,
            kind="group" if payload["isGroup"] else "dm",
            native_thread_id=None,
            native_root_id=None,
        )
        context = ChatContext(
            channel_id=f"adapter:whatsapp:{self.instance.instance_id}:{chat_jid}",
            source_chat=chat_ref,
            title=None,
            sender=sender,
            audience=AudienceSnapshot(
                kind="group" if payload["isGroup"] else "private",
                participants=(sender,),
                completeness="unknown" if payload["isGroup"] else "complete",
                correspondence_id=None,
                policy_version=1,
            ),
            mentions=(),
            available_intents=(),
            capabilities_revision="tryx:no-account-reviewed:v1",
        )
        platform = WhatsAppProvenanceV1(
            schema="hyprial.chat.platform.whatsapp/v1",
            scope=PlatformScopeV1(
                organization_id=self.instance.organization_id,
                instance_id=self.instance.instance_id,
                generation=self.instance.generation,
                account_scope=account_jid,
                environment="linked-device",
            ),
            refs=PlatformRefsV1(
                message_id=message_id,
                reply_id=quoted_id,
                attachment_refs=tuple(handle.handle_id for handle in attachments),
            ),
            unknown=unknown,
            account_jid=account_jid,
            chat_jid=chat_jid,
            participant_jid=participant_jid,
            message_id=message_id,
            device_id=device_id,
            quoted_message_id=quoted_id,
            wrappers=wrappers,
            view_once=payload["viewOnce"],
            is_from_me=is_from_me,
            media_refs=tuple(handle.handle_id for handle in attachments),
        )
        text = payload["text"]
        content: list[object] = []
        if isinstance(text, str) and text:
            content.append(TextBlock("text", text))
        content.extend(AttachmentBlock("attachment", handle) for handle in attachments)
        if not content:
            content.append(
                UnsupportedNativeBlock(
                    "unsupported-native",
                    str(payload["eventKind"]),
                    "WhatsApp native event without agent-readable content",
                    event.envelope.observation.raw_safe_ref,
                )
            )
        reply = (
            ReplyReference(None, quoted_id, None, (), (), None) if quoted_id is not None else None
        )
        provenance = SourceProvenance(
            ingress_id=f"whatsapp:{self.instance.instance_id}:{message_id}",
            observation_ids=(event.envelope.observation.observation_id,),
            aliases=event.envelope.observation.aliases,
            received_at_ms=int(payload["receivedAtMs"]),
            provider_created_at_ms=int(payload["providerCreatedAtMs"]),
            origin="agent-output" if is_from_me else "native-human",
            native_sender_id=sender_jid,
        )
        return WhatsAppNormalizedIngress(
            context=context,
            provenance=provenance,
            content=tuple(content),
            attachments=attachments,
            reply_to=reply,
            platform=platform,
        )

    def _attachments(self, value: object, message_id: str) -> tuple[AttachmentHandle, ...]:
        result: list[AttachmentHandle] = []
        for item in _sequence(value, "media"):
            if not isinstance(item, Mapping) or set(item) != {
                "ref",
                "kind",
                "name",
                "mediaType",
                "size",
                "sha256",
            }:
                raise ValueError("invalid media descriptor")
            kind = item["kind"]
            if kind not in _MEDIA_KINDS:
                raise ValueError("invalid media kind")
            result.append(
                AttachmentHandle(
                    handle_id=_text(item["ref"], "media.ref"),
                    kind=kind,
                    name=item["name"] if isinstance(item["name"], str) else None,
                    media_type=item["mediaType"] if isinstance(item["mediaType"], str) else None,
                    size=item["size"] if isinstance(item["size"], int) else None,
                    sha256=item["sha256"] if isinstance(item["sha256"], str) else None,
                    source_message_id=message_id,
                    expires_at_ms=None,
                )
            )
        return tuple(result)

    def render(self, target: NativeTargetResolution, message: ChatMessage | object) -> OperationDraft:
        if target.status != "resolved" or not target.native_chat_id:
            raise WhatsAppProviderError("WHATSAPP_TARGET_UNRESOLVED", "target is not resolved")
        payload = getattr(message, "payload", None)
        content = getattr(payload, "content", getattr(message, "content", ()))
        parent_event_id = getattr(message, "parent_event_id", None)
        nodes: list[OperationDraftNode] = []
        texts: list[str] = []
        for block in content:
            if isinstance(block, AttachmentBlock):
                upload_name = f"upload-{len(nodes)}"
                template = {
                    "chatJid": target.native_chat_id,
                    "handleId": block.handle.handle_id,
                }
                if parent_event_id is not None:
                    template["quotedMessageId"] = parent_event_id
                nodes.append(
                    OperationDraftNode(
                        upload_name,
                        "file-upload",
                        (),
                        template,
                    )
                )
            elif isinstance(block, TextBlock):
                texts.append(block.text)
        if texts:
            template = {"chatJid": target.native_chat_id, "text": "\n".join(texts)}
            if parent_event_id is not None:
                template["quotedMessageId"] = parent_event_id
            nodes.append(OperationDraftNode(f"text-{len(nodes)}", "text-send", (), template))
        if not nodes:
            raise WhatsAppProviderError("WHATSAPP_CONTENT_UNSUPPORTED", "no renderable content")
        materialized = freeze_json_v1([node.template for node in nodes])
        return OperationDraft(
            renderer_revision="whatsapp-linked-device-renderer/v1",
            capability_revision="tryx:no-account-reviewed:v1",
            materialized_content_digest=frozen_json_digest_v1(materialized),
            nodes=tuple(nodes),
        )


__all__ = ["WhatsAppCodec", "WhatsAppNormalizedIngress"]
