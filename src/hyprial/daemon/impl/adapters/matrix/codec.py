"""Pure Matrix event normalization and immutable operation rendering."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from ..contracts import (
    AdapterInstanceRef,
    AttachmentBlock,
    AttachmentHandle,
    AudienceSnapshot,
    ChatContext,
    ChatMessage,
    ChatRef,
    HydratedEvent,
    IntentCapability,
    MatrixProvenanceV1,
    MentionBlock,
    NativeTargetResolution,
    NormalizedIngress,
    OperationDraft,
    OperationDraftNode,
    PlatformRefsV1,
    PlatformScopeV1,
    PlatformUnknownV1,
    PrincipalRef,
    ReplyReference,
    RichTextBlock,
    SourceProvenance,
    TextBlock,
    UnsupportedNativeBlock,
    canonical_json_v1,
    frozen_json_digest_v1,
)
from .errors import MatrixCryptoBlocked
from .models import read_room_encryption_evidence


CAPABILITY_REVISION = "matrix-client-v1"
RENDERER_REVISION = "matrix-renderer-v1"


def _map(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Matrix {name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Matrix {name} is required")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _principal(user_id: str) -> PrincipalRef:
    return PrincipalRef(
        provider_subject_id=user_id,
        open_id=None,
        union_id=None,
        display_name=None,
        hyprial_principal=None,
        resolution="unresolved",
        proof_ref=None,
        is_self=None,
        standing=None,
        source="matrix-client-server-api",
    )


def _intents() -> tuple[IntentCapability, ...]:
    return (
        IntentCapability("chat.reply.source", ("inherit", "corresponding", "exact"), True),
        IntentCapability("chat.reply.private", ("inherit", "corresponding", "exact"), True),
        IntentCapability("chat.send.explicit", ("corresponding", "exact"), False),
    )


def _attachment_kind(msgtype: str) -> str:
    return {
        "m.image": "image",
        "m.audio": "audio",
        "m.video": "video",
        "m.file": "file",
    }.get(msgtype, "other")


def _message_blocks(message: ChatMessage | object) -> tuple[object, ...]:
    blocks = getattr(message, "content", None)
    if blocks is None and hasattr(message, "payload"):
        blocks = getattr(message.payload, "content", ())
    return tuple(blocks or ())


class MatrixEventCodec:
    """Decode bounded Matrix timeline events without network or credential I/O."""

    def __init__(self, instance: AdapterInstanceRef, *, encrypted_rooms: Sequence[str] = ()) -> None:
        if instance.kind != "matrix" or instance.variant != "client":
            raise ValueError("Matrix codec requires a matrix/client instance")
        if encrypted_rooms:
            raise ValueError("Matrix encrypted rooms require authoritative resolution evidence")
        self._instance = instance

    def normalize(self, event: HydratedEvent) -> NormalizedIngress:
        return self.decode(event)[0]

    def decode(self, event: HydratedEvent) -> tuple[NormalizedIngress, MatrixProvenanceV1]:
        native = _map(json.loads(canonical_json_v1(event.body)), "timeline event")
        event_type = _text(native.get("type"), "event type")
        event_id = _text(native.get("event_id"), "event id")
        room_id = _text(native.get("_hyprial_room_id"), "room id")
        sender_id = _text(native.get("sender"), "sender")
        content = _map(native.get("content"), "content")
        encrypted = native.get("_hyprial_encrypted") is True
        if event_type == "m.room.encrypted":
            raise MatrixCryptoBlocked("matrix-e2e-ciphertext-not-decrypted")

        relation = content.get("m.relates_to")
        relation_map = relation if isinstance(relation, Mapping) else {}
        relation_type = _optional_text(relation_map.get("rel_type"))
        relates_to = _optional_text(relation_map.get("event_id"))
        reply_map = relation_map.get("m.in_reply_to")
        reply_id = (
            _optional_text(reply_map.get("event_id")) if isinstance(reply_map, Mapping) else None
        )
        thread_root = relates_to if relation_type == "m.thread" else None
        transaction_id = None
        unsigned = native.get("unsigned")
        if isinstance(unsigned, Mapping):
            transaction_id = _optional_text(unsigned.get("transaction_id"))

        sender = _principal(sender_id)
        direct = native.get("_hyprial_is_direct") is True
        chat_kind = "thread" if thread_root else "dm" if direct else "group"
        account_scope = f"{native.get('_hyprial_homeserver')}|{native.get('_hyprial_user_id')}"
        chat_ref = ChatRef(
            ref_id=(
                f"chat-ref:v1:matrix:{self._instance.instance_id}:{room_id}:"
                f"{thread_root or '-'}"
            ),
            instance=self._instance,
            account_scope=account_scope,
            native_chat_id=room_id,
            kind=chat_kind,
            native_thread_id=thread_root,
            native_root_id=thread_root,
        )
        context = ChatContext(
            channel_id=f"adapter:matrix:{self._instance.instance_id}:{room_id}",
            source_chat=chat_ref,
            title=_optional_text(native.get("_hyprial_room_name")),
            sender=sender,
            audience=AudienceSnapshot(
                "private" if direct else "group", (sender,), "partial", None, 1
            ),
            mentions=(),
            available_intents=_intents(),
            capabilities_revision=CAPABILITY_REVISION,
        )

        msgtype = _optional_text(content.get("msgtype"))
        body = _optional_text(content.get("body"))
        media_mxc: list[str] = []
        attachments: list[AttachmentHandle] = []
        blocks: list[object] = []
        media_url = _optional_text(content.get("url"))
        if media_url is not None:
            if not media_url.startswith("mxc://"):
                raise ValueError("Matrix media URL must use mxc://")
            media_mxc.append(media_url)
            info = content.get("info") if isinstance(content.get("info"), Mapping) else {}
            size = info.get("size") if isinstance(info.get("size"), int) else None
            handle = AttachmentHandle(
                handle_id=f"matrix:{self._instance.instance_id}:{media_url}",
                kind=_attachment_kind(msgtype or ""),
                name=body,
                media_type=_optional_text(info.get("mimetype")),
                size=size if size is None or size >= 0 else None,
                sha256=None,
                source_message_id=event_id,
                expires_at_ms=None,
            )
            attachments.append(handle)
            blocks.append(AttachmentBlock("attachment", handle))
        elif event_type == "m.room.message" and body:
            blocks.append(TextBlock("text", body))
        elif event_type == "m.reaction":
            blocks.append(
                UnsupportedNativeBlock(
                    "unsupported-native", "m.reaction", "Matrix reaction", event_id
                )
            )
        else:
            blocks.append(
                UnsupportedNativeBlock(
                    "unsupported-native", event_type, "Unsupported Matrix event", event_id
                )
            )

        observation = event.envelope.observation
        provenance = SourceProvenance(
            ingress_id=f"ingress:matrix:{self._instance.instance_id}:{event_id}",
            observation_ids=(observation.observation_id,),
            aliases=observation.aliases,
            received_at_ms=observation.received_at_ms,
            provider_created_at_ms=(
                native.get("origin_server_ts")
                if isinstance(native.get("origin_server_ts"), int)
                else None
            ),
            origin="bridge-mirror" if transaction_id else "native-human",
            native_sender_id=sender_id,
        )
        refs = PlatformRefsV1(
            event_id=event_id,
            message_id=event_id,
            reply_id=reply_id,
            thread_id=thread_root,
            attachment_refs=tuple(media_mxc),
        )
        platform = MatrixProvenanceV1(
            schema="hyprial.chat.platform.matrix/v1",
            scope=PlatformScopeV1(
                self._instance.organization_id,
                self._instance.instance_id,
                self._instance.generation,
                account_scope,
                _text(native.get("_hyprial_homeserver"), "homeserver"),
            ),
            refs=refs,
            unknown=PlatformUnknownV1({}, observation.raw_safe_ref),
            homeserver=_text(native.get("_hyprial_homeserver"), "homeserver"),
            user_id=_text(native.get("_hyprial_user_id"), "user id"),
            device_id=_optional_text(native.get("_hyprial_device_id")),
            room_id=room_id,
            event_id=event_id,
            txn_id=transaction_id,
            relation_type=relation_type,
            relates_to=relates_to,
            thread_root=thread_root,
            encrypted=encrypted,
            media_mxc=tuple(media_mxc),
        )
        reply = (
            ReplyReference(None, reply_id, None, (), (), "not-delivered")
            if reply_id is not None
            else None
        )
        return (
            NormalizedIngress(context, provenance, tuple(blocks), tuple(attachments), reply),
            platform,
        )

    def render(
        self, target: NativeTargetResolution, message: ChatMessage | object
    ) -> OperationDraft:
        if target.status != "resolved" or target.native_chat_id is None:
            raise ValueError("Matrix render requires a resolved target")
        if target.target.chat_ref.instance != self._instance:
            raise ValueError("Matrix target instance mismatch")
        encrypted = read_room_encryption_evidence(
            target.reason_code,
            self._instance.instance_id,
            self._instance.generation,
            target.native_chat_id,
        )
        attachments = tuple(getattr(message, "attachments", ()))
        if attachments and encrypted:
            raise MatrixCryptoBlocked("matrix-encrypted-attachments-unsupported")
        text_parts: list[str] = []
        for block in _message_blocks(message):
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, RichTextBlock):
                text_parts.append(block.plain_fallback)
            elif isinstance(block, MentionBlock):
                text_parts.append(block.label)
        body = "\n".join(text_parts).strip()
        if not body and not attachments:
            raise ValueError("Matrix message requires text or an attachment")
        relation: dict[str, object] | None = None
        if target.native_thread_id:
            relation = {"rel_type": "m.thread", "event_id": target.native_thread_id}
        elif target.canonical_parent_id:
            relation = {"m.in_reply_to": {"event_id": target.canonical_parent_id}}
        content: dict[str, object] = {"msgtype": "m.text", "body": body}
        if relation is not None:
            content["m.relates_to"] = relation
        template = {
            "provider": "matrix",
            "organization_id": self._instance.organization_id,
            "instance_id": self._instance.instance_id,
            "generation": self._instance.generation,
            "room_id": target.native_chat_id,
            "event_type": "m.room.message",
            "content": content,
            "encrypted": encrypted,
            "room_encryption_evidence": target.reason_code,
            "part_index": len(attachments),
        }
        nodes: list[OperationDraftNode] = []
        for index, attachment in enumerate(attachments):
            nodes.append(
                OperationDraftNode(
                    f"upload-{index}",
                    "file-upload",
                    (),
                    {
                        "provider": "matrix",
                        "organization_id": self._instance.organization_id,
                        "instance_id": self._instance.instance_id,
                        "generation": self._instance.generation,
                        "room_id": target.native_chat_id,
                        "handle_id": attachment.handle_id,
                        "encrypted": False,
                        "room_encryption_evidence": target.reason_code,
                        "part_index": index,
                    },
                )
            )
        nodes.append(
            OperationDraftNode(
                "message",
                "text-send" if not attachments else "file-send",
                tuple(node.local_name for node in nodes),
                template,
            )
        )
        return OperationDraft(
            RENDERER_REVISION,
            CAPABILITY_REVISION,
            frozen_json_digest_v1(template),
            tuple(nodes),
        )


__all__ = ["CAPABILITY_REVISION", "MatrixEventCodec", "RENDERER_REVISION"]
