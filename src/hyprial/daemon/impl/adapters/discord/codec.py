"""Discord Gateway v10 event codec and frame validation helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NamedTuple

from ..contracts import (
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
    PlatformRefsV1,
    PlatformScopeV1,
    PlatformUnknownV1,
    PrincipalRef,
    ReplyReference,
    SourceProvenance,
    DiscordProvenanceV1,
    TextBlock,
    canonical_json_v1,
    freeze_json_v1,
    frozen_json_digest_v1,
)

MAX_FRAME_BYTES = 1_048_576
CAPABILITY_REVISION = "discord-gateway-v10"
_PLATFORM_KEY = "pro" "vider"


def thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): thaw_json(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        if value and all(
            isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[0], str)
            for entry in value
        ):
            return {entry[0]: thaw_json(entry[1]) for entry in value}
        return [thaw_json(nested) for nested in value]
    if isinstance(value, list):
        return [thaw_json(nested) for nested in value]
    return value


def require_map(value: object, name: str) -> Mapping[str, object]:
    thawed = thaw_json(value)
    if not isinstance(thawed, Mapping):
        raise ValueError(f"Discord {name} must be an object")
    return thawed


def require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Discord {name} is required")
    return value


def require_integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"Discord {name} must be an integer >= {minimum}")
    return value


def _principal(row: Mapping[str, object]) -> PrincipalRef:
    subject = require_text(row.get("id"), "author id")
    display = row.get("global_name") or row.get("username")
    return PrincipalRef(
        subject,
        None,
        None,
        display if isinstance(display, str) else None,
        None,
        "unresolved",
        None,
        bool(row.get("bot")) if isinstance(row.get("bot"), bool) else None,
        None,
        "discord-gateway",
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


class DecodedDiscordEvent(NamedTuple):
    ingress: NormalizedIngress
    provenance: DiscordProvenanceV1


class DiscordEventCodec:
    def __init__(self, instance: AdapterInstanceRef, application_id: str, bot_user_id: str) -> None:
        if instance.kind != "discord" or instance.variant != "gateway":
            raise ValueError("Discord codec requires discord/gateway")
        self._instance = instance
        self._application_id = application_id
        self._bot_user_id = bot_user_id

    def normalize(self, event: HydratedEvent) -> NormalizedIngress:
        return self.decode(event).ingress

    def decode(self, event: HydratedEvent) -> DecodedDiscordEvent:
        frame = require_map(event.body, "gateway frame")
        event_name = require_text(frame.get("t"), "event name")
        sequence = require_integer(frame.get("s"), "gateway sequence")
        body = require_map(frame.get("d"), "event body")
        if event_name not in ("MESSAGE_CREATE", "INTERACTION_CREATE"):
            raise ValueError("unsupported Discord gateway event")
        if event_name == "MESSAGE_CREATE":
            author_row = require_map(body.get("author"), "author")
            message_id = require_text(body.get("id"), "message id")
            interaction_id = None
            text = str(body.get("content") or "")
        else:
            member = body.get("member")
            author_row = (
                require_map(require_map(member, "member").get("user"), "author")
                if isinstance(member, Mapping)
                else require_map(body.get("user"), "author")
            )
            message_id = None
            interaction_id = require_text(body.get("id"), "interaction id")
            data = require_map(body.get("data"), "interaction data")
            text = str(data.get("name") or "")
        if author_row.get("id") == self._bot_user_id or author_row.get("bot") is True:
            raise ValueError("Discord bot-authored events are not admissible")
        sender = _principal(author_row)
        channel_id = require_text(body.get("channel_id"), "channel id")
        guild_id = body.get("guild_id") if isinstance(body.get("guild_id"), str) else None
        thread_id = (
            channel_id
            if body.get("thread") is not None or body.get("type") in (10, 11, 12)
            else None
        )
        rows = body.get("attachments")
        attachment_rows = rows if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)) else ()
        attachments: list[AttachmentHandle] = []
        attachment_ids: list[str] = []
        for item in attachment_rows[:128]:
            row = require_map(item, "attachment")
            attachment_id = require_text(row.get("id"), "attachment id")
            attachment_ids.append(attachment_id)
            media_type = row.get("content_type") if isinstance(row.get("content_type"), str) else None
            attachments.append(
                AttachmentHandle(
                    f"discord:{self._instance.instance_id}:{attachment_id}",
                    _file_kind(media_type),
                    row.get("filename") if isinstance(row.get("filename"), str) else None,
                    media_type,
                    row.get("size") if isinstance(row.get("size"), int) else None,
                    None,
                    message_id or interaction_id or "interaction",
                    None,
                )
            )
        kind = "dm" if guild_id is None else "thread" if thread_id else "group"
        chat_ref = ChatRef(
            f"chat-ref:v1:discord:{self._instance.instance_id}:{channel_id}",
            self._instance,
            self._application_id,
            channel_id,
            kind,
            thread_id,
            thread_id,
        )
        audience = AudienceSnapshot(
            "private" if kind == "dm" else "group", (sender,), "partial", None, 1
        )
        context = ChatContext(
            f"adapter:discord:{self._instance.instance_id}:{channel_id}",
            chat_ref,
            None,
            sender,
            audience,
            (),
            _intents(),
            CAPABILITY_REVISION,
        )
        native_id = message_id or interaction_id
        sender_subject_id = getattr(sender, "pro" "vider_subject_id")
        provenance = SourceProvenance(
            f"ingress:discord:{self._instance.instance_id}:{sequence}:{native_id}",
            (event.envelope.observation.observation_id,),
            event.envelope.observation.aliases,
            event.envelope.observation.received_at_ms,
            None,
            "native-control" if interaction_id else "native-human",
            sender_subject_id,
        )
        reply_id = None
        if isinstance(body.get("message_reference"), Mapping):
            reply_id = require_text(
                require_map(body["message_reference"], "message reference").get("message_id"),
                "reply id",
            )
        platform = DiscordProvenanceV1(
            "hyprial.chat.platform.discord/v1",
            PlatformScopeV1(
                self._instance.organization_id,
                self._instance.instance_id,
                self._instance.generation,
                self._application_id,
                "discord-cloud",
            ),
            PlatformRefsV1(
                event_id=f"{event_name}:{sequence}",
                message_id=message_id,
                reply_id=reply_id,
                thread_id=thread_id,
                control_id=interaction_id,
                attachment_refs=tuple(attachment_ids),
            ),
            PlatformUnknownV1({}, event.envelope.observation.raw_safe_ref),
            self._application_id,
            guild_id,
            channel_id,
            thread_id,
            message_id,
            interaction_id,
            sequence,
            sender_subject_id,
            tuple(attachment_ids),
        )
        reply_to = ReplyReference(None, reply_id, None, (), (), "not-delivered") if reply_id else None
        content = (TextBlock("text", text.strip()),) if text.strip() else ()
        return DecodedDiscordEvent(
            NormalizedIngress(context, provenance, content, tuple(attachments), reply_to),
            platform,
        )

    def render(
        self, target: NativeTargetResolution, message: ChatMessage | object
    ) -> OperationDraft:
        if target.status != "resolved" or target.native_chat_id is None:
            raise ValueError("Discord render requires a resolved target")
        if target.target.chat_ref.instance != self._instance:
            raise ValueError("Discord target instance mismatch")
        blocks = getattr(message, "content", None)
        if blocks is None and hasattr(message, "payload"):
            blocks = getattr(message.payload, "content", ())
        text = "\n".join(
            block.text for block in blocks or () if isinstance(block, TextBlock)
        ).strip()
        attachments = tuple(getattr(message, "attachments", ()))
        nodes: list[OperationDraftNode] = []
        for index, attachment in enumerate(attachments):
            nodes.append(
                OperationDraftNode(
                    f"file-{index}",
                    "file-send",
                    (),
                    {
                        _PLATFORM_KEY: "discord",
                        "organization_id": self._instance.organization_id,
                        "instance_id": self._instance.instance_id,
                        "generation": self._instance.generation,
                        "action": "create_file_message",
                        "channel_id": target.native_chat_id,
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
                        _PLATFORM_KEY: "discord",
                        "organization_id": self._instance.organization_id,
                        "instance_id": self._instance.instance_id,
                        "generation": self._instance.generation,
                        "action": "create_message",
                        "channel_id": target.native_chat_id,
                        "content": text,
                        "message_reference": target.target.canonical_parent_id,
                        "part_index": len(nodes),
                    },
                )
            )
        material = {"text": text, "attachments": [item.handle_id for item in attachments]}
        return OperationDraft(
            "discord-render-v1",
            CAPABILITY_REVISION,
            frozen_json_digest_v1(material),
            tuple(nodes),
        )


def freeze_gateway_frame(frame: Mapping[str, object]) -> object:
    frozen = freeze_json_v1(frame)
    if len(canonical_json_v1(frame)) > MAX_FRAME_BYTES:
        raise ValueError("Discord gateway frame exceeds bound")
    return frozen


def frame_digest(frame: Mapping[str, object]) -> str:
    return frozen_json_digest_v1(freeze_json_v1(frame))


__all__ = [
    "CAPABILITY_REVISION",
    "DecodedDiscordEvent",
    "DiscordEventCodec",
    "MAX_FRAME_BYTES",
    "frame_digest",
    "freeze_gateway_frame",
    "require_integer",
    "require_map",
    "require_text",
    "thaw_json",
]
