from __future__ import annotations

import pytest

from hyprial.daemon.impl.adapters.contracts import (
    AttachmentBlock,
    HydratedEvent,
    TextBlock,
    WhatsAppProvenanceV1,
)

from .support import envelope, instance, native_event, target


def _codec():
    from hyprial.daemon.impl.adapters.whatsapp import WhatsAppCodec

    return WhatsAppCodec(instance(), account_jid="15550001@s.whatsapp.net")


def test_normalize_preserves_whatsapp_group_reply_wrapper_media_and_alias_provenance() -> None:
    event = envelope()
    normalized = _codec().normalize(HydratedEvent(event, event.payload))

    assert isinstance(normalized.platform, WhatsAppProvenanceV1)
    assert normalized.platform.account_jid == "15550001@s.whatsapp.net"
    assert normalized.platform.chat_jid == "120363000000@g.us"
    assert normalized.platform.participant_jid == "15550002@s.whatsapp.net"
    assert normalized.platform.quoted_message_id == "wamid-parent"
    assert normalized.platform.wrappers == ("ephemeral", "view-once-v2")
    assert normalized.platform.view_once is True
    assert normalized.platform.media_refs == ("native-media:wa-main:wamid-1:0",)
    assert normalized.context.source_chat.kind == "group"
    assert normalized.context.channel_id.startswith("adapter:whatsapp:")
    assert normalized.context.sender.provider_subject_id == "15550002@s.whatsapp.net"
    assert normalized.context.sender.is_self is False
    assert normalized.provenance.aliases == event.observation.aliases
    assert normalized.reply_to.source_native_message_id == "wamid-parent"
    assert [type(block) for block in normalized.content] == [TextBlock, AttachmentBlock]


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("accountJid", "other@s.whatsapp.net", "WHATSAPP_ACCOUNT_MISMATCH"),
        ("wrappers", ["x"] * 129, "WHATSAPP_EVENT_INVALID"),
        ("unknown", {"accessToken": "secret"}, "WHATSAPP_EVENT_INVALID"),
        ("messageId", "", "WHATSAPP_EVENT_INVALID"),
    ],
)
def test_normalize_fails_closed_on_wrong_scope_bounds_secrets_and_missing_ids(
    field: str, value: object, code: str
) -> None:
    payload = native_event()
    payload[field] = value
    with pytest.raises(Exception) as raised:
        _codec().normalize(HydratedEvent(envelope(payload), payload))
    assert getattr(raised.value, "code", None) == code


def test_render_materializes_exact_driver_reply_and_single_media_send_operation() -> None:
    from hyprial.daemon.impl.adapters.contracts import (
        AttachmentHandle,
        ChatReplyIntent,
        IntentPayload,
    )

    attachment = AttachmentHandle(
        handle_id="attachment:wa:1",
        kind="image",
        name="photo.jpg",
        media_type="image/jpeg",
        size=5,
        sha256="0" * 64,
        source_message_id="wamid-parent",
        expires_at_ms=None,
    )
    intent = ChatReplyIntent(
        schema="hyprial.chat.intent.chat-reply/v1",
        payload=IntentPayload(
            intent_id="intent-1",
            content=(TextBlock("text", "reply"), AttachmentBlock("attachment", attachment)),
            attachments=(attachment.handle_id,),
            idempotency_key="idem:intent-1",
        ),
        parent_event_id="event-parent",
        mode="source-audience",
        sync="exact",
    )

    draft = _codec().render(
        type("Resolution", (), {"status": "resolved", "native_chat_id": target().chat_ref.native_chat_id})(),
        intent,
    )
    assert [node.kind for node in draft.nodes] == ["file-upload", "text-send"]
    assert draft.nodes[0].template == (
        ("chatJid", "120363000000@g.us"),
        ("handleId", "attachment:wa:1"),
        ("quotedMessageId", "event-parent"),
    )
    assert draft.nodes[1].template == (
        ("chatJid", "120363000000@g.us"),
        ("quotedMessageId", "event-parent"),
        ("text", "reply"),
    )
