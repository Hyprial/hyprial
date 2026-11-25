"""Slack Socket Mode protocol logic behind injectable authenticated I/O ports."""

from __future__ import annotations

import html
from collections.abc import Sequence

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
    SlackProvenanceV1,
    SourceProvenance,
    TextBlock,
    frozen_json_digest_v1,
)
from .components import (
    MAX_FRAME_BYTES,
    MAX_PENDING_ACKS,
    SlackOnboarding,
    SlackSocketRuntime,
)
from .errors import SlackHttpResult, SlackProtocolError
from .types import (
    SlackCheckpointLedger,
    SlackCredentialResolver,
    SlackCredentials,
    SlackTransport,
    _CAPABILITY_REVISION,
    _integer,
    _map,
    _text,
)



def _file_kind(media_type: str | None) -> str:
    if media_type and media_type.startswith("image/"):
        return "image"
    if media_type and media_type.startswith("audio/"):
        return "audio"
    if media_type and media_type.startswith("video/"):
        return "video"
    return "file"


def _principal(subject: str, display_name: str | None = None) -> PrincipalRef:
    return PrincipalRef(
        provider_subject_id=subject,
        open_id=None,
        union_id=None,
        display_name=display_name,
        hyprial_principal=None,
        resolution="unresolved",
        proof_ref=None,
        is_self=None,
        standing=None,
        source="slack-events-api",
    )


def _intents() -> tuple[IntentCapability, ...]:
    return (
        IntentCapability(
            "chat.reply.source", ("inherit", "corresponding", "exact"), True
        ),
        IntentCapability(
            "chat.reply.private", ("inherit", "corresponding", "exact"), True
        ),
        IntentCapability("chat.send.explicit", ("corresponding", "exact"), False),
    )


def _content_text(message: ChatMessage | object) -> str:
    blocks = getattr(message, "content", None)
    if blocks is None and hasattr(message, "payload"):
        blocks = getattr(message.payload, "content", ())
    return "\n".join(
        block.text for block in blocks or () if isinstance(block, TextBlock)
    ).strip()


class SlackEventCodec:
    """Pure Slack event normalization and operation rendering."""

    def __init__(
        self, instance: AdapterInstanceRef, team_id: str, bot_user_id: str
    ) -> None:
        if instance.kind != "slack" or instance.variant != "socket-mode":
            raise ValueError("Slack codec requires a slack/socket-mode instance")
        self._instance = instance
        self._team_id = _text(team_id, "team id")
        self._bot_user_id = _text(bot_user_id, "bot user id")

    def normalize(self, event: HydratedEvent) -> NormalizedIngress:
        return self.decode(event)[0]

    def decode(
        self, event: HydratedEvent
    ) -> tuple[NormalizedIngress, SlackProvenanceV1]:
        frame = _map(event.body, "event frame")
        payload = _map(frame.get("payload"), "events payload")
        native = _map(payload.get("event"), "native event")
        event_id = _text(payload.get("event_id"), "event id")
        event_type = _text(native.get("type"), "event type")
        if event_type not in ("message", "app_mention"):
            raise ValueError("unsupported Slack event type")
        if native.get("bot_id") or native.get("app_id"):
            raise ValueError("Slack bot-authored events are not admissible")
        subtype = native.get("subtype")
        if subtype not in (None, "file_share"):
            raise ValueError("unsupported Slack message subtype")
        channel_id = _text(native.get("channel"), "channel id")
        user_id = _text(native.get("user"), "user id")
        message_ts = _text(native.get("ts"), "message timestamp")
        thread_ts = (
            native.get("thread_ts")
            if isinstance(native.get("thread_ts"), str)
            else None
        )
        direct = native.get("channel_type") == "im"
        if not direct and event_type != "app_mention":
            raise ValueError("Slack group input must address the app")
        files = native.get("files")
        file_rows = (
            files
            if isinstance(files, Sequence) and not isinstance(files, (str, bytes))
            else ()
        )
        attachments: list[AttachmentHandle] = []
        file_ids: list[str] = []
        for item in file_rows[:128]:
            row = _map(item, "file")
            file_id = _text(row.get("id"), "file id")
            file_ids.append(file_id)
            media_type = (
                row.get("mimetype") if isinstance(row.get("mimetype"), str) else None
            )
            size = (
                row.get("size")
                if isinstance(row.get("size"), int) and row.get("size") >= 0
                else None
            )
            attachments.append(
                AttachmentHandle(
                    handle_id=f"slack:{self._instance.instance_id}:{file_id}",
                    kind=_file_kind(media_type),
                    name=row.get("name") if isinstance(row.get("name"), str) else None,
                    media_type=media_type,
                    size=size,
                    sha256=None,
                    source_message_id=message_ts,
                    expires_at_ms=None,
                )
            )
        sender = _principal(user_id)
        chat_kind = "dm" if direct else "thread" if thread_ts else "group"
        chat_ref = ChatRef(
            ref_id=f"chat-ref:v1:slack:{self._instance.instance_id}:{channel_id}:{thread_ts or '-'}",
            instance=self._instance,
            account_scope=self._team_id,
            native_chat_id=channel_id,
            kind=chat_kind,
            native_thread_id=thread_ts,
            native_root_id=thread_ts,
        )
        audience = AudienceSnapshot(
            "private" if direct else "group", (sender,), "partial", None, 1
        )
        context = ChatContext(
            channel_id=f"adapter:slack:{self._instance.instance_id}:{channel_id}",
            source_chat=chat_ref,
            title=None,
            sender=sender,
            audience=audience,
            mentions=(),
            available_intents=_intents(),
            capabilities_revision=_CAPABILITY_REVISION,
        )
        aliases = event.envelope.observation.aliases
        provenance = SourceProvenance(
            ingress_id=f"ingress:slack:{self._instance.instance_id}:{event_id}",
            observation_ids=(event.envelope.observation.observation_id,),
            aliases=aliases,
            received_at_ms=event.envelope.observation.received_at_ms,
            provider_created_at_ms=(
                _integer(payload["event_time"], "event time") * 1000
                if "event_time" in payload
                else None
            ),
            origin="native-human",
            native_sender_id=user_id,
        )
        refs = PlatformRefsV1(
            event_id=event_id,
            message_id=message_ts,
            reply_id=thread_ts,
            thread_id=thread_ts,
            attachment_refs=tuple(file_ids),
        )
        platform = SlackProvenanceV1(
            schema="hyprial.chat.platform.slack/v1",
            scope=PlatformScopeV1(
                self._instance.organization_id,
                self._instance.instance_id,
                self._instance.generation,
                self._team_id,
                "slack-cloud",
            ),
            refs=refs,
            unknown=PlatformUnknownV1({}, event.envelope.observation.raw_safe_ref),
            team_id=self._team_id,
            enterprise_id=(
                payload.get("enterprise_id")
                if isinstance(payload.get("enterprise_id"), str)
                else None
            ),
            channel_id=channel_id,
            event_id=event_id,
            event_ts=str(native.get("event_ts") or message_ts),
            message_ts=message_ts,
            thread_ts=thread_ts,
            subtype=subtype if isinstance(subtype, str) else None,
            user_id=user_id,
            file_ids=tuple(file_ids),
        )
        reply_to = None
        if thread_ts and thread_ts != message_ts:
            reply_to = ReplyReference(None, thread_ts, None, (), (), "not-delivered")
        raw_text = html.unescape(str(native.get("text") or ""))
        text = raw_text.replace(f"<@{self._bot_user_id}>", "").strip()
        content = (TextBlock("text", text),) if text else ()
        return (
            NormalizedIngress(
                context, provenance, content, tuple(attachments), reply_to
            ),
            platform,
        )

    def render(
        self, target: NativeTargetResolution, message: ChatMessage | object
    ) -> OperationDraft:
        if target.status != "resolved" or target.native_chat_id is None:
            raise ValueError("Slack render requires a resolved target")
        if target.target.chat_ref.instance != self._instance:
            raise ValueError("Slack target instance mismatch")
        text = _content_text(message)
        attachments = tuple(getattr(message, "attachments", ()))
        nodes: list[OperationDraftNode] = []
        upload_names: list[str] = []
        for index, attachment in enumerate(attachments):
            name = f"upload-{index}"
            upload_names.append(name)
            nodes.append(
                OperationDraftNode(
                    name,
                    "file-upload",
                    (),
                    {
                        "provider": "slack",
                        "organization_id": self._instance.organization_id,
                        "instance_id": self._instance.instance_id,
                        "generation": self._instance.generation,
                        "method": "files.uploadV2",
                        "channel": target.native_chat_id,
                        "thread_ts": target.native_thread_id,
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
                    tuple(upload_names),
                    {
                        "provider": "slack",
                        "organization_id": self._instance.organization_id,
                        "instance_id": self._instance.instance_id,
                        "generation": self._instance.generation,
                        "method": "chat.postMessage",
                        "channel": target.native_chat_id,
                        "thread_ts": target.native_thread_id,
                        "text": text,
                        "part_index": len(nodes),
                    },
                )
            )
        material = {
            "text": text,
            "attachments": [item.handle_id for item in attachments],
        }
        return OperationDraft(
            "slack-render-v1",
            _CAPABILITY_REVISION,
            frozen_json_digest_v1(material),
            tuple(nodes),
        )



__all__ = [
    "MAX_FRAME_BYTES",
    "MAX_PENDING_ACKS",
    "SlackCheckpointLedger",
    "SlackCredentialResolver",
    "SlackCredentials",
    "SlackEventCodec",
    "SlackHttpResult",
    "SlackOnboarding",
    "SlackProtocolError",
    "SlackSocketRuntime",
    "SlackTransport",
]
