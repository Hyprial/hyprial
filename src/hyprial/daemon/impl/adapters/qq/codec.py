"""QQ official-bot ingress codec and passive-only operation rendering."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    AttachmentHandle,
    AudienceSnapshot,
    ChatContext,
    ChatMessage,
    ChatRef,
    HydratedEvent,
    IntentCapability,
    NativeTargetResolution,
    NormalizedIngress,
    OperationDraft,
    OperationDraftNode,
    PrincipalRef,
    QQProvenanceV1,
    PlatformRefsV1,
    PlatformScopeV1,
    PlatformUnknownV1,
    ReplyReference,
    SourceProvenance,
    TextBlock,
    frozen_json_digest_v1,
)

CAPABILITY_REVISION = "qq-official-bot-v1"
_MENTION = re.compile(r"<@!?[^>]+>")


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        if value and all(
            isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[0], str)
            for entry in value
        ):
            return {entry[0]: _thaw(entry[1]) for entry in value}
        return [_thaw(nested) for nested in value]
    if isinstance(value, list):
        return [_thaw(nested) for nested in value]
    return value


def _map(value: object, name: str) -> Mapping[str, object]:
    thawed = _thaw(value)
    if not isinstance(thawed, Mapping):
        raise ValueError(f"QQ {name} must be an object")
    return thawed


def _text(value: object, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"QQ {name} is required")
    result = str(value)
    if not result:
        raise ValueError(f"QQ {name} is required")
    return result


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"QQ {name} must be an integer >= {minimum}")
    return value


def _principal(row: Mapping[str, object]) -> PrincipalRef:
    subject = _text(row.get("id") or row.get("user_openid") or row.get("member_openid"), "author id")
    return PrincipalRef(
        subject,
        None,
        None,
        row.get("username") if isinstance(row.get("username"), str) else None,
        None,
        "unresolved",
        None,
        None,
        None,
        "qq-official-gateway",
    )


def _intents() -> tuple[IntentCapability, ...]:
    return (
        IntentCapability("chat.reply.source", ("inherit", "corresponding", "exact"), True),
        IntentCapability("chat.reply.private", ("inherit", "corresponding", "exact"), True),
        IntentCapability("chat.send.explicit", ("corresponding", "exact"), False),
    )


def _file_kind(media_type: str | None) -> str:
    if media_type and media_type.startswith("image/"):
        return "image"
    if media_type and media_type.startswith("audio/"):
        return "audio"
    if media_type and media_type.startswith("video/"):
        return "video"
    return "file"


class QQEventCodec:
    """Decode official gateway events; render passive-only operation drafts."""

    def __init__(self, instance: AdapterInstanceRef, app_id: str, bot_id: str) -> None:
        if instance.kind != "qq" or instance.variant != "official-bot":
            raise ValueError("QQ codec requires qq/official-bot")
        self._instance = instance
        self._app_id = app_id
        self._bot_id = bot_id

    def normalize(self, event: HydratedEvent) -> NormalizedIngress:
        return self.decode(event)[0]

    def decode(self, event: HydratedEvent) -> tuple[NormalizedIngress, QQProvenanceV1]:
        frame = _map(event.body, "gateway frame")
        event_name = _text(frame.get("t"), "event name")
        if event_name not in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
            raise ValueError("unsupported QQ official bot event")
        _integer(frame.get("s"), "gateway sequence")
        body = _map(frame.get("d"), "event body")
        event_id = _text(body.get("event_id") or frame.get("id"), "event id")
        message_id = _text(body.get("id"), "message id")
        sender = _principal(_map(body.get("author"), "author"))
        native_sender_id = getattr(sender, "provider_subject_id")
        scene = "group" if event_name == "GROUP_AT_MESSAGE_CREATE" else "c2c"
        group_open_id = (
            _text(body.get("group_openid") or body.get("group_open_id"), "group open id")
            if scene == "group"
            else None
        )
        native_chat_id = group_open_id or native_sender_id
        attachment_rows = body.get("attachments")
        rows = (
            attachment_rows
            if isinstance(attachment_rows, Sequence) and not isinstance(attachment_rows, (str, bytes))
            else ()
        )
        attachments: list[AttachmentHandle] = []
        media_refs: list[str] = []
        for index, item in enumerate(rows[:128]):
            row = _map(item, "attachment")
            media_id = _text(row.get("id") or row.get("url") or f"index-{index}", "media id")
            media_refs.append(media_id)
            media_type = (
                row.get("content_type")
                if isinstance(row.get("content_type"), str)
                else row.get("contentType") if isinstance(row.get("contentType"), str) else None
            )
            attachments.append(
                AttachmentHandle(
                    f"qq:{self._instance.instance_id}:{media_id}",
                    _file_kind(media_type),
                    row.get("filename") if isinstance(row.get("filename"), str) else None,
                    media_type,
                    row.get("size") if isinstance(row.get("size"), int) else None,
                    None,
                    message_id,
                    None,
                )
            )
        chat_ref = ChatRef(
            f"chat-ref:v1:qq:{self._instance.instance_id}:{scene}:{native_chat_id}",
            self._instance,
            self._app_id,
            native_chat_id,
            "group" if scene == "group" else "dm",
            None,
            None,
        )
        audience = AudienceSnapshot(
            "group" if scene == "group" else "private", (sender,), "partial", None, 1
        )
        context = ChatContext(
            f"adapter:qq:{self._instance.instance_id}:{native_chat_id}",
            chat_ref,
            None,
            sender,
            audience,
            (),
            _intents(),
            CAPABILITY_REVISION,
        )
        provenance = SourceProvenance(
            f"ingress:qq:{self._instance.instance_id}:{event_id}",
            (event.envelope.observation.observation_id,),
            event.envelope.observation.aliases,
            event.envelope.observation.received_at_ms,
            None,
            "native-human",
            native_sender_id,
        )
        reference = body.get("message_reference")
        if not isinstance(reference, Mapping) and isinstance(body.get("messageReference"), Mapping):
            reference = body.get("messageReference")
        reply_id = (
            _text(_map(reference, "message reference").get("message_id"), "reply id")
            if isinstance(reference, Mapping)
            else body.get("ref_msg_idx") if isinstance(body.get("ref_msg_idx"), str) else None
        )
        msg_seq = body.get("msg_seq")
        if msg_seq is not None:
            msg_seq = _integer(msg_seq, "message sequence")
        platform = QQProvenanceV1(
            "hyprial.chat.platform.qq/v1",
            PlatformScopeV1(
                self._instance.organization_id,
                self._instance.instance_id,
                self._instance.generation,
                self._app_id,
                "qq-official",
            ),
            PlatformRefsV1(
                event_id=event_id,
                message_id=message_id,
                reply_id=reply_id,
                attachment_refs=tuple(media_refs),
            ),
            PlatformUnknownV1({}, event.envelope.observation.raw_safe_ref),
            self._app_id,
            scene,
            native_sender_id,
            group_open_id,
            event_id,
            message_id,
            reply_id,
            msg_seq,
            tuple(media_refs),
        )
        reply_to = ReplyReference(None, reply_id, None, (), (), "not-delivered") if reply_id else None
        content = _MENTION.sub("", str(body.get("content") or "")).strip()
        blocks = (TextBlock("text", content),) if content else ()
        return (
            NormalizedIngress(context, provenance, blocks, tuple(attachments), reply_to),
            platform,
        )

    def render(self, target: NativeTargetResolution, message: ChatMessage | object) -> OperationDraft:
        if target.status != "resolved" or target.native_chat_id is None:
            raise ValueError("QQ render requires a resolved target")
        if target.target.chat_ref.instance != self._instance:
            raise ValueError("QQ target instance mismatch")
        msg_id = target.target.canonical_parent_id
        if not isinstance(msg_id, str) or not msg_id:
            raise ValueError("QQ passive-only send requires an inbound msg_id")
        blocks = getattr(message, "content", None)
        if blocks is None and hasattr(message, "payload"):
            blocks = getattr(message.payload, "content", ())
        text = "\n".join(block.text for block in blocks or () if isinstance(block, TextBlock)).strip()
        attachments = tuple(getattr(message, "attachments", ()))
        nodes: list[OperationDraftNode] = []
        for index, attachment in enumerate(attachments):
            nodes.append(
                OperationDraftNode(
                    f"media-{index}",
                    "file-send",
                    (),
                    {
                        "provider": "qq",
                        "organization_id": self._instance.organization_id,
                        "instance_id": self._instance.instance_id,
                        "generation": self._instance.generation,
                        "action": "send_media",
                        "scene": "group" if target.target.chat_ref.kind == "group" else "c2c",
                        "target_id": target.native_chat_id,
                        "msg_id": msg_id,
                        "handle_id": attachment.handle_id,
                        "part_index": index,
                    },
                )
            )
        if text or not nodes:
            nodes.append(
                OperationDraftNode(
                    "message",
                    "text-send",
                    (),
                    {
                        "provider": "qq",
                        "organization_id": self._instance.organization_id,
                        "instance_id": self._instance.instance_id,
                        "generation": self._instance.generation,
                        "action": "send_message",
                        "scene": "group" if target.target.chat_ref.kind == "group" else "c2c",
                        "target_id": target.native_chat_id,
                        "content": text,
                        "msg_id": msg_id,
                        "part_index": len(nodes),
                    },
                )
            )
        material = {"text": text, "attachments": [item.handle_id for item in attachments]}
        return OperationDraft(
            "qq-render-v1", CAPABILITY_REVISION, frozen_json_digest_v1(material), tuple(nodes)
        )


__all__ = ["CAPABILITY_REVISION", "QQEventCodec", "thaw_json"]


def thaw_json(value: object) -> object:
    """Convert frozen JSON values back to plain dicts/lists for wire access."""
    return _thaw(value)
